"""Unit tests for the pure-logic helpers in ``media_toolkit.videos.concat``.

These tests intentionally exercise only the pieces that do not need ffmpeg
or ffprobe to be installed; the thin subprocess wrappers are out of scope
here and are validated manually in milestone C.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pysrt
import pytest

from media_toolkit.videos import concat as concat_module
from media_toolkit.videos.concat import (
    EXIT_SETUP_ERROR,
    REENCODE_TOLERANCE_MS,
    STREAM_COPY_TOLERANCE_MS,
    DuplicateSectionError,
    MissingDependencyError,
    Section,
    StreamCopyFailedError,
    _resolve_binary,
    build_concat_list_text,
    build_timestamps_text,
    concat_videos,
    discover_sections,
    format_timestamp,
    merge_srts,
    section_sort_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_srt(path: Path, cues: list[tuple[str, str, str]]) -> None:
    """Write a minimal SRT file from (start, end, text) tuples (HH:MM:SS,ms)."""
    blocks: list[str] = []
    for index, (start, end, text) in enumerate(cues, start=1):
        blocks.append(f"{index}\n{start} --> {end}\n{text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


# ---------------------------------------------------------------------------
# section_sort_key
# ---------------------------------------------------------------------------


def test_section_sort_key_natural_order() -> None:
    filenames = ["1-0 a.mp4", "1-1 a.mp4", "1-10 a.mp4", "1-2 a.mp4"]
    ordered = sorted(filenames, key=section_sort_key)
    assert ordered == ["1-0 a.mp4", "1-1 a.mp4", "1-2 a.mp4", "1-10 a.mp4"]


def test_section_sort_key_rejects_non_section_filename() -> None:
    with pytest.raises(ValueError):
        section_sort_key("notes.txt")


# ---------------------------------------------------------------------------
# format_timestamp
# ---------------------------------------------------------------------------


def test_format_timestamp() -> None:
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(356) == "00:05:56"
    assert format_timestamp(3661) == "01:01:01"


def test_format_timestamp_zero_pads_hours_above_ten() -> None:
    assert format_timestamp(36000) == "10:00:00"


# ---------------------------------------------------------------------------
# build_concat_list_text
# ---------------------------------------------------------------------------


def test_build_concat_list_text_two_paths(tmp_path: Path) -> None:
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"")
    b.write_bytes(b"")
    text = build_concat_list_text([a, b])
    assert text == f"file '{a.resolve()}'\nfile '{b.resolve()}'\n"


def test_build_concat_list_text_escapes_single_quotes(tmp_path: Path) -> None:
    tricky = tmp_path / "it's tricky.mp4"
    tricky.write_bytes(b"")
    text = build_concat_list_text([tricky])
    expected_path = str(tricky.resolve()).replace("'", "''")
    assert text == f"file '{expected_path}'\n"
    # Sanity: there should be exactly one occurrence of the doubled quote.
    assert "''" in text


# ---------------------------------------------------------------------------
# merge_srts
# ---------------------------------------------------------------------------


def test_merge_srts_shifts_correctly(tmp_path: Path) -> None:
    srt_a = tmp_path / "a.srt"
    srt_b = tmp_path / "b.srt"
    _write_srt(
        srt_a,
        [
            ("00:00:01,000", "00:00:02,000", "hello-a-1"),
            ("00:00:03,000", "00:00:04,000", "hello-a-2"),
        ],
    )
    _write_srt(
        srt_b,
        [
            ("00:00:05,000", "00:00:06,000", "hello-b-1"),
        ],
    )

    output = tmp_path / "merged.srt"
    merge_srts([srt_a, srt_b], [300_000, 180_000], output)

    merged = pysrt.open(str(output), encoding="utf-8")
    assert [item.index for item in merged] == [1, 2, 3]
    # Third cue (first cue of srt_b) should be shifted by 300_000ms (5 minutes).
    third = merged[2]
    assert third.text == "hello-b-1"
    assert third.start.ordinal == 5_000 + 300_000
    assert third.end.ordinal == 6_000 + 300_000


def test_merge_srts_handles_missing_lang(tmp_path: Path) -> None:
    srt_b = tmp_path / "b.srt"
    _write_srt(
        srt_b,
        [
            ("00:00:05,000", "00:00:06,000", "only-b"),
        ],
    )
    output = tmp_path / "merged.srt"
    merge_srts([None, srt_b], [300_000, 180_000], output)

    merged = pysrt.open(str(output), encoding="utf-8")
    assert len(merged) == 1
    only = merged[0]
    assert only.index == 1
    assert only.text == "only-b"
    # Shift = sum of preceding durations = 300_000ms.
    assert only.start.ordinal == 5_000 + 300_000


def test_merge_srts_length_mismatch_raises(tmp_path: Path) -> None:
    output = tmp_path / "merged.srt"
    with pytest.raises(ValueError):
        merge_srts([None, None], [1000], output)


# ---------------------------------------------------------------------------
# build_timestamps_text
# ---------------------------------------------------------------------------


def test_build_timestamps_text() -> None:
    sections = [
        Section(key=(1, 0), label="1-0 alpha", mp4_path=Path("a.mp4"), srts={}),
        Section(key=(1, 1), label="1-1 bravo", mp4_path=Path("b.mp4"), srts={}),
        Section(key=(1, 2), label="1-2 charlie", mp4_path=Path("c.mp4"), srts={}),
    ]
    text = build_timestamps_text(sections, [300_000, 356_000, 200_000])
    assert text == (
        "00:00:00 1-0 alpha\n"
        "00:05:00 1-1 bravo\n"
        "00:10:56 1-2 charlie\n"
    )


# ---------------------------------------------------------------------------
# discover_sections
# ---------------------------------------------------------------------------


def test_discover_sections_groups_by_section(tmp_path: Path) -> None:
    files = [
        "1-0 foo.mp4",
        "1-0 foo.zh-TW.srt",
        "1-0 foo.ja.srt",
        "1-1 bar.mp4",
        "1-1 bar.zh-TW.srt",
        "notes.txt",  # ignored
    ]
    for name in files:
        (tmp_path / name).write_bytes(b"")

    sections = discover_sections(tmp_path)
    assert [s.key for s in sections] == [(1, 0), (1, 1)]
    assert sections[0].label == "1-0 foo"
    assert sections[1].label == "1-1 bar"
    assert set(sections[0].srts.keys()) == {"zh-TW", "ja"}
    assert set(sections[1].srts.keys()) == {"zh-TW"}
    assert sections[0].srts["zh-TW"].name == "1-0 foo.zh-TW.srt"


def test_discover_sections_natural_order_across_ten(tmp_path: Path) -> None:
    for name in ["1-0 a.mp4", "1-1 a.mp4", "1-2 a.mp4", "1-10 a.mp4"]:
        (tmp_path / name).write_bytes(b"")
    sections = discover_sections(tmp_path)
    assert [s.key for s in sections] == [(1, 0), (1, 1), (1, 2), (1, 10)]


def test_discover_sections_detects_duplicates(tmp_path: Path) -> None:
    # Two distinct files sharing the same (1, 0) key - mirrors the
    # wordup "double-space vs full-width slash" stale-duplicate scenario.
    (tmp_path / "1-0 foo  bar.mp4").write_bytes(b"")  # double-space variant
    (tmp_path / "1-0 foo ／ bar.mp4").write_bytes(b"")  # full-width slash
    with pytest.raises(DuplicateSectionError):
        discover_sections(tmp_path)


def test_discover_sections_three_way_duplicate_lists_all_files(
    tmp_path: Path,
) -> None:
    """When 3+ files collide on one key, all three names must appear in the error."""
    names = [
        "1-0 alpha.mp4",
        "1-0 bravo.mp4",
        "1-0 charlie.mp4",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"")

    with pytest.raises(DuplicateSectionError) as exc_info:
        discover_sections(tmp_path)

    message = str(exc_info.value)
    for name in names:
        assert name in message, f"expected {name!r} in error message:\n{message}"


def test_discover_sections_missing_dir_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(FileNotFoundError):
        discover_sections(missing)


# ---------------------------------------------------------------------------
# Binary resolver / dependency check
# ---------------------------------------------------------------------------


def _reset_resolver_cache() -> None:
    """Clear the module-level resolved-binary cache so tests are isolated."""
    concat_module._RESOLVED_FFMPEG_BIN = None
    concat_module._RESOLVED_FFPROBE_BIN = None


def test_resolve_binary_falls_back_to_snap_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``ffprobe`` is absent but ``ffmpeg.ffprobe`` exists (snap layout),
    the resolver picks the snap-mangled name."""

    def fake_which(name: str) -> str | None:
        if name == "ffprobe":
            return None
        if name == "ffmpeg.ffprobe":
            return "/snap/bin/ffmpeg.ffprobe"
        return None

    monkeypatch.setattr(concat_module.shutil, "which", fake_which)
    assert _resolve_binary(("ffprobe", "ffmpeg.ffprobe")) == "ffmpeg.ffprobe"


def test_resolve_binary_raises_when_none_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no candidate resolves, raise MissingDependencyError listing all
    candidate names so the user can see exactly what was tried."""
    monkeypatch.setattr(concat_module.shutil, "which", lambda _name: None)

    with pytest.raises(MissingDependencyError) as exc_info:
        _resolve_binary(("ffprobe", "ffmpeg.ffprobe"))

    message = str(exc_info.value)
    assert "ffprobe" in message
    assert "ffmpeg.ffprobe" in message


def test_run_returns_setup_error_when_ffprobe_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If ffprobe (under any candidate name) is unavailable, ``run`` must exit
    with EXIT_SETUP_ERROR (2), not silently return 0."""
    _reset_resolver_cache()

    # ffmpeg present, but neither ffprobe candidate resolves.
    def fake_which(name: str) -> str | None:
        if name == "ffmpeg":
            return "/usr/bin/ffmpeg"
        return None

    monkeypatch.setattr(concat_module.shutil, "which", fake_which)

    args = argparse.Namespace(
        input_dir=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        reencode_on_failure=False,
        quiet=True,
    )
    rc = concat_module.run(args)
    assert rc == EXIT_SETUP_ERROR

    _reset_resolver_cache()


# ---------------------------------------------------------------------------
# concat_videos — TS-intermediate flow + silent-truncation detection
#
# These tests mock the three ffmpeg/ffprobe wrappers (`_convert_source_to_ts`,
# `_run_ts_concat`, and `get_duration_ms`) at the module level so they
# exercise the orchestration logic without requiring a real ffmpeg install.
# Each call's ``reencode`` flag (for converts) is captured so tests can
# assert on the order of passes (stream-copy first, then re-encode).
# ---------------------------------------------------------------------------


# Three dummy inputs with a known total duration. The actual files don't
# need to exist; concat_videos only touches them via the (mocked) wrappers
# and the workdir creation, which goes under tmp_path (real fs).
_INPUT_DURATIONS_MS: list[int] = [300_000, 200_000, 100_000]
_EXPECTED_TOTAL_MS: int = sum(_INPUT_DURATIONS_MS)


def _setup_ts_concat_mocks(
    monkeypatch: pytest.MonkeyPatch,
    convert_rc_sequence: list[int],
    concat_rc_sequence: list[int],
    duration_sequence: list[int],
) -> dict[str, list[dict[str, object]]]:
    """Patch the new TS helpers + ``get_duration_ms`` with deterministic sequences.

    Returns a dict with two call-log lists:
      - ``convert``: one dict per ``_convert_source_to_ts`` call (captures
        ``reencode`` flag and ``src``/``dest_ts`` paths). The mock writes a
        zero-byte file at ``dest_ts`` so the workdir mirrors what real
        ffmpeg would produce (lets the cleanup glob find the segments).
      - ``concat``: one dict per ``_run_ts_concat`` call (captures the list
        of segment paths and ``output_path``).
    """
    calls: dict[str, list[dict[str, object]]] = {"convert": [], "concat": []}
    convert_iter = iter(convert_rc_sequence)
    concat_iter = iter(concat_rc_sequence)
    duration_iter = iter(duration_sequence)

    def fake_convert(src: Path, dest_ts: Path, reencode: bool) -> int:
        calls["convert"].append(
            {"src": src, "dest_ts": dest_ts, "reencode": reencode}
        )
        rc = next(convert_iter)
        if rc == 0:
            # Mirror the real helper: produce the segment file on disk so
            # the workdir cleanup glob behaves identically to a real run.
            dest_ts.parent.mkdir(parents=True, exist_ok=True)
            dest_ts.write_bytes(b"")
        return rc

    def fake_concat(ts_segments: list[Path], output_path: Path) -> int:
        calls["concat"].append(
            {"segments": list(ts_segments), "output_path": output_path}
        )
        rc = next(concat_iter)
        if rc == 0:
            # Same idea: produce a placeholder so unlink() in the retry
            # path has something to remove.
            output_path.write_bytes(b"")
        return rc

    def fake_get_duration(_path: Path) -> int:
        return next(duration_iter)

    monkeypatch.setattr(concat_module, "_convert_source_to_ts", fake_convert)
    monkeypatch.setattr(concat_module, "_run_ts_concat", fake_concat)
    monkeypatch.setattr(concat_module, "get_duration_ms", fake_get_duration)
    return calls


def test_concat_videos_passes_when_duration_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TS convert + concat both succeed with rc=0; merged duration matches
    sum-of-inputs. Expect no exception, exactly one convert per source and
    one concat call, all with reencode=False."""
    calls = _setup_ts_concat_mocks(
        monkeypatch,
        convert_rc_sequence=[0, 0, 0],
        concat_rc_sequence=[0],
        duration_sequence=[_EXPECTED_TOTAL_MS],
    )

    inputs = [tmp_path / f"in{i}.mp4" for i in range(3)]
    output = tmp_path / "merged.mp4"

    concat_videos(
        inputs,
        output,
        source_durations_ms=_INPUT_DURATIONS_MS,
        reencode_on_failure=False,
    )

    assert len(calls["convert"]) == 3
    assert all(call["reencode"] is False for call in calls["convert"])
    assert len(calls["concat"]) == 1


def test_concat_videos_raises_on_silent_truncation_without_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """rc=0 but merged duration is half of expected (classic silent
    truncation): with reencode_on_failure=False, raise StreamCopyFailedError
    so the user knows to re-run with the fallback flag."""
    calls = _setup_ts_concat_mocks(
        monkeypatch,
        convert_rc_sequence=[0, 0, 0],
        concat_rc_sequence=[0],
        # Half the expected total — well outside STREAM_COPY_TOLERANCE_MS.
        duration_sequence=[_EXPECTED_TOTAL_MS // 2],
    )

    inputs = [tmp_path / f"in{i}.mp4" for i in range(3)]
    output = tmp_path / "merged.mp4"

    with pytest.raises(StreamCopyFailedError) as exc_info:
        concat_videos(
            inputs,
            output,
            source_durations_ms=_INPUT_DURATIONS_MS,
            reencode_on_failure=False,
        )

    message = str(exc_info.value)
    assert "silent truncation" in message
    assert "--reencode-on-failure" in message
    # Only the stream-copy pass should have run.
    assert len(calls["convert"]) == 3
    assert all(call["reencode"] is False for call in calls["convert"])
    assert len(calls["concat"]) == 1


def test_concat_videos_falls_back_to_reencode_on_silent_truncation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First stream-copy pass returns rc=0 but truncated duration; with
    reencode_on_failure=True, the second pass (reencode=True) produces the
    correct duration. Expect no exception, two passes observed; the second
    pass's converts must all have reencode=True."""
    calls = _setup_ts_concat_mocks(
        monkeypatch,
        # First pass: 3 converts (stream-copy). Second pass: 3 converts (reencode).
        convert_rc_sequence=[0, 0, 0, 0, 0, 0],
        concat_rc_sequence=[0, 0],
        duration_sequence=[
            _EXPECTED_TOTAL_MS // 3,  # truncated stream-copy
            _EXPECTED_TOTAL_MS,       # correct re-encode
        ],
    )

    inputs = [tmp_path / f"in{i}.mp4" for i in range(3)]
    output = tmp_path / "merged.mp4"

    concat_videos(
        inputs,
        output,
        source_durations_ms=_INPUT_DURATIONS_MS,
        reencode_on_failure=True,
    )

    assert len(calls["convert"]) == 6
    # First three calls = stream-copy pass.
    assert all(call["reencode"] is False for call in calls["convert"][:3])
    # Last three calls = re-encode pass.
    assert all(call["reencode"] is True for call in calls["convert"][3:])
    assert len(calls["concat"]) == 2


def test_concat_videos_falls_back_to_reencode_on_concat_rc_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the concat-protocol step returns non-zero, fall through to
    re-encode. Verifies the rc!=0 path still triggers the fallback after
    the TS-intermediate refactor."""
    calls = _setup_ts_concat_mocks(
        monkeypatch,
        convert_rc_sequence=[0, 0, 0, 0, 0, 0],
        # First concat fails (rc=1), second succeeds (rc=0).
        concat_rc_sequence=[1, 0],
        # Only the re-encode call probes duration (rc=1 short-circuits).
        duration_sequence=[_EXPECTED_TOTAL_MS],
    )

    inputs = [tmp_path / f"in{i}.mp4" for i in range(3)]
    output = tmp_path / "merged.mp4"

    concat_videos(
        inputs,
        output,
        source_durations_ms=_INPUT_DURATIONS_MS,
        reencode_on_failure=True,
    )

    assert len(calls["convert"]) == 6
    assert all(call["reencode"] is False for call in calls["convert"][:3])
    assert all(call["reencode"] is True for call in calls["convert"][3:])
    assert len(calls["concat"]) == 2


def test_concat_videos_raises_when_reencode_also_truncates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both passes return rc=0 but both produce truncated output (delta >
    REENCODE_TOLERANCE_MS): raise StreamCopyFailedError rather than
    silently returning a broken file."""
    calls = _setup_ts_concat_mocks(
        monkeypatch,
        convert_rc_sequence=[0, 0, 0, 0, 0, 0],
        concat_rc_sequence=[0, 0],
        duration_sequence=[
            _EXPECTED_TOTAL_MS // 3,  # stream-copy truncated
            _EXPECTED_TOTAL_MS // 2,  # re-encode also truncated
        ],
    )

    inputs = [tmp_path / f"in{i}.mp4" for i in range(3)]
    output = tmp_path / "merged.mp4"

    with pytest.raises(StreamCopyFailedError) as exc_info:
        concat_videos(
            inputs,
            output,
            source_durations_ms=_INPUT_DURATIONS_MS,
            reencode_on_failure=True,
        )

    assert "re-encode produced wrong duration" in str(exc_info.value)
    assert len(calls["convert"]) == 6
    assert all(call["reencode"] is False for call in calls["convert"][:3])
    assert all(call["reencode"] is True for call in calls["convert"][3:])
    assert len(calls["concat"]) == 2


def test_concat_videos_cleans_up_workdir_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The .concat-work directory must be removed after a successful run so
    we don't leak ~hundreds of MB of TS segments per chapter."""
    _setup_ts_concat_mocks(
        monkeypatch,
        convert_rc_sequence=[0, 0, 0],
        concat_rc_sequence=[0],
        duration_sequence=[_EXPECTED_TOTAL_MS],
    )

    inputs = [tmp_path / f"in{i}.mp4" for i in range(3)]
    output = tmp_path / "merged.mp4"

    concat_videos(
        inputs,
        output,
        source_durations_ms=_INPUT_DURATIONS_MS,
        reencode_on_failure=False,
    )

    assert not (tmp_path / concat_module.TS_WORKDIR_NAME).exists()


def test_concat_videos_tolerance_constants_are_sensible() -> None:
    """Sanity check on the threshold constants — re-encode should tolerate
    at least as much drift as stream-copy, and both should be small enough
    to catch real truncation (which is on the order of seconds-to-minutes)."""
    assert STREAM_COPY_TOLERANCE_MS > 0
    assert REENCODE_TOLERANCE_MS >= STREAM_COPY_TOLERANCE_MS
    # Both should be well under any plausible truncation amount (the bug
    # the user hit dropped ~58 minutes; either tolerance flags that easily).
    assert STREAM_COPY_TOLERANCE_MS < 60_000
    assert REENCODE_TOLERANCE_MS < 60_000
