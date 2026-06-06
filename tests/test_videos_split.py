"""Unit tests for ``media_toolkit.videos.split``.

Pure-logic helpers are exercised directly (no ffmpeg/ffprobe needed).
The ``run()`` entry point is tested via monkeypatching of
``_ffmpeg.get_duration_ms``, ``split.split_video``, and questionary.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from media_toolkit.videos import split as split_module
from media_toolkit.videos.split import (
    EXIT_ITEM_FAILED,
    EXIT_OK,
    EXIT_SETUP_ERROR,
    EXIT_USER_ABORT,
    MIN_PARTS,
    SECONDS_TO_MS,
    _MODE_EQUAL_LABEL,
    _MODE_SEGMENT_LABEL,
    _METHOD_STREAM_COPY_LABEL,
    _METHOD_REENCODE_LABEL,
    build_output_pattern,
    compute_equal_split_points,
    compute_segment_time_points,
    expected_part_count,
    format_segment_times,
    run,
)


# ---------------------------------------------------------------------------
# compute_equal_split_points
# ---------------------------------------------------------------------------


def test_equal_split_four_parts_known_value() -> None:
    """7766692 ms / 4 parts → 3 interior points at the reference values."""
    duration_ms = 7_766_692
    points = compute_equal_split_points(duration_ms, 4)
    assert points == [1_941_673, 3_883_346, 5_825_019]


def test_equal_split_two_parts() -> None:
    """2 parts → 1 interior point at the midpoint."""
    points = compute_equal_split_points(10_000, 2)
    assert len(points) == 1
    assert points[0] == 5_000


def test_equal_split_exact_divisible() -> None:
    """Duration that divides evenly: no rounding artefacts."""
    points = compute_equal_split_points(9_000, 3)
    assert points == [3_000, 6_000]


def test_equal_split_parts_less_than_two_raises() -> None:
    with pytest.raises(ValueError, match="at least"):
        compute_equal_split_points(10_000, 1)


def test_equal_split_parts_zero_raises() -> None:
    with pytest.raises(ValueError, match="at least"):
        compute_equal_split_points(10_000, 0)


def test_equal_split_parts_negative_raises() -> None:
    with pytest.raises(ValueError, match="at least"):
        compute_equal_split_points(10_000, -1)


def test_equal_split_zero_duration_raises() -> None:
    with pytest.raises(ValueError, match="positive"):
        compute_equal_split_points(0, 4)


def test_equal_split_negative_duration_raises() -> None:
    with pytest.raises(ValueError, match="positive"):
        compute_equal_split_points(-1_000, 4)


def test_equal_split_returns_parts_minus_one_points() -> None:
    """Always returns exactly parts-1 points regardless of duration."""
    for parts in range(2, 8):
        points = compute_equal_split_points(100_000, parts)
        assert len(points) == parts - 1


# ---------------------------------------------------------------------------
# compute_segment_time_points
# ---------------------------------------------------------------------------


def test_segment_time_clean_division() -> None:
    """9000ms / 3000ms chunks → 2 interior points."""
    points = compute_segment_time_points(9_000, 3_000)
    assert points == [3_000, 6_000]


def test_segment_time_non_clean_remainder() -> None:
    """10000ms / 3000ms → 3 points; last chunk is 1000ms (shorter)."""
    points = compute_segment_time_points(10_000, 3_000)
    assert points == [3_000, 6_000, 9_000]


def test_segment_time_segment_equals_duration_raises() -> None:
    with pytest.raises(ValueError, match="single part"):
        compute_segment_time_points(5_000, 5_000)


def test_segment_time_segment_greater_than_duration_raises() -> None:
    with pytest.raises(ValueError, match="single part"):
        compute_segment_time_points(5_000, 10_000)


def test_segment_time_zero_segment_raises() -> None:
    with pytest.raises(ValueError, match="positive"):
        compute_segment_time_points(10_000, 0)


def test_segment_time_negative_segment_raises() -> None:
    with pytest.raises(ValueError, match="positive"):
        compute_segment_time_points(10_000, -500)


def test_segment_time_one_chunk_below_duration() -> None:
    """Just below duration → empty interior points list (degenerate, but valid)."""
    # segment_ms < duration_ms → one cut point would be at segment_ms,
    # but 4999 < 5000, so cursor = 4999, then 9998 >= 5000 stops.
    points = compute_segment_time_points(5_000, 4_999)
    assert points == [4_999]


# ---------------------------------------------------------------------------
# format_segment_times
# ---------------------------------------------------------------------------


def test_format_segment_times_single_point() -> None:
    assert format_segment_times([1_941_673]) == "1941.673"


def test_format_segment_times_multiple_points() -> None:
    assert format_segment_times([1_941_673, 3_883_346]) == "1941.673,3883.346"


def test_format_segment_times_empty_list() -> None:
    assert format_segment_times([]) == ""


def test_format_segment_times_three_decimals() -> None:
    """Ensure output always has exactly 3 decimal places."""
    result = format_segment_times([1_000])
    assert result == "1.000"


def test_format_segment_times_non_round_ms() -> None:
    """1001ms → 1.001s (3 decimals, no truncation)."""
    assert format_segment_times([1_001]) == "1.001"


# ---------------------------------------------------------------------------
# build_output_pattern
# ---------------------------------------------------------------------------


def test_build_output_pattern_stem_and_suffix_preserved(tmp_path: Path) -> None:
    video = tmp_path / "myvideo.mp4"
    out_dir = tmp_path / "out"
    pattern = build_output_pattern(video, out_dir)
    assert pattern.endswith("myvideo_part%d.mp4")


def test_build_output_pattern_percent_d_present(tmp_path: Path) -> None:
    video = tmp_path / "clip.mkv"
    pattern = build_output_pattern(video, tmp_path)
    assert "%d" in pattern


def test_build_output_pattern_output_dir_used(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    out_dir = tmp_path / "subdir"
    pattern = build_output_pattern(video, out_dir)
    # Pattern must be rooted in out_dir.
    assert pattern.startswith(str(out_dir))


def test_build_output_pattern_cjk_space_paren_filename(tmp_path: Path) -> None:
    """CJK / space / paren / special chars in stem must pass through intact."""
    video = tmp_path / "(自购)【1080P】test JS~1.mp4"
    out_dir = tmp_path / "output"
    pattern = build_output_pattern(video, out_dir)
    # Stem preserved verbatim (no sanitization expected by the pure helper).
    assert "(自购)【1080P】test JS~1_part%d.mp4" in pattern
    assert pattern.endswith(".mp4")


def test_build_output_pattern_mkv_suffix(tmp_path: Path) -> None:
    video = tmp_path / "lecture.mkv"
    pattern = build_output_pattern(video, tmp_path)
    assert pattern.endswith("lecture_part%d.mkv")


# ---------------------------------------------------------------------------
# expected_part_count
# ---------------------------------------------------------------------------


def test_expected_part_count_zero_points() -> None:
    assert expected_part_count([]) == 1


def test_expected_part_count_one_point() -> None:
    assert expected_part_count([5_000]) == 2


def test_expected_part_count_three_points() -> None:
    assert expected_part_count([1_000, 2_000, 3_000]) == 4


# ---------------------------------------------------------------------------
# run() — monkeypatched end-to-end tests
# ---------------------------------------------------------------------------


def _base_args(**overrides: object) -> argparse.Namespace:
    """Return a fully-populated argparse.Namespace with safe defaults for run()."""
    ns = argparse.Namespace(
        input=None,
        output_dir=None,
        parts=None,
        segment_time=None,
        reencode=False,
        yes=True,   # skip questionary.confirm by default in tests
        quiet=False,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def _patch_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    duration_ms: int = 10_000,
    split_rc: int = 0,
) -> dict[str, list[object]]:
    """Patch ffmpeg / ffprobe resolution + split_video for run() tests.

    Also patches ``normalize_path_input`` in the split module so that
    Windows-style ``tmp_path`` strings are returned as-is instead of being
    translated to a non-existent ``/mnt/c/...`` WSL path.  This mirrors how
    the watermark tests handle the same env-specific issue (the failing
    watermark tests are pre-existing and accepted; here we avoid the problem
    entirely so all split tests pass on Windows).

    Returns a call-log dict with:
    - ``split``: list of kwargs dicts passed to split_video
    """
    calls: dict[str, list[object]] = {"split": []}

    # Bypass WSL path translation so tmp_path files are found on Windows.
    monkeypatch.setattr(split_module, "normalize_path_input", lambda v: Path(v) if v is not None else None)

    # Patch binary resolution helpers imported into split.py's namespace.
    # resolve_ffmpeg_bin / resolve_ffprobe_bin are called directly in run().
    monkeypatch.setattr(split_module, "resolve_ffmpeg_bin", lambda: "ffmpeg")
    monkeypatch.setattr(split_module, "resolve_ffprobe_bin", lambda: "ffprobe")

    # Patch get_duration_ms in split.py's namespace (imported via
    # ``from media_toolkit._ffmpeg import get_duration_ms``).
    monkeypatch.setattr(split_module, "get_duration_ms", lambda _path: duration_ms)

    # Patch split_video so no real ffmpeg is invoked.
    def fake_split_video(
        input_path: Path,
        output_pattern: str,
        segment_times_str: str,
        reencode: bool,
    ) -> int:
        calls["split"].append(
            {
                "input_path": input_path,
                "output_pattern": output_pattern,
                "segment_times_str": segment_times_str,
                "reencode": reencode,
            }
        )
        return split_rc

    monkeypatch.setattr(split_module, "split_video", fake_split_video)

    return calls


# --- mutually exclusive validation ---


def test_run_both_parts_and_segment_time_returns_setup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "a.mp4"
    video.write_bytes(b"")
    _patch_dependencies(monkeypatch, tmp_path)
    args = _base_args(input=str(video), parts=4, segment_time=30.0)

    rc = run(args)

    assert rc == EXIT_SETUP_ERROR


def test_run_neither_parts_nor_segment_time_returns_setup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "a.mp4"
    video.write_bytes(b"")
    _patch_dependencies(monkeypatch, tmp_path)
    args = _base_args(input=str(video))  # parts=None, segment_time=None

    rc = run(args)

    assert rc == EXIT_SETUP_ERROR


# --- missing input file ---


def test_run_missing_input_file_returns_setup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dependencies(monkeypatch, tmp_path)
    args = _base_args(
        input=str(tmp_path / "nonexistent.mp4"),
        parts=4,
    )

    rc = run(args)

    assert rc == EXIT_SETUP_ERROR


# --- happy path: equal parts ---


def test_run_equal_parts_calls_split_video_with_correct_segment_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() with --parts 4 and 7766692ms duration must call split_video
    with the expected segment_times string."""
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"")
    calls = _patch_dependencies(monkeypatch, tmp_path, duration_ms=7_766_692)
    args = _base_args(input=str(video), parts=4, yes=True)

    rc = run(args)

    assert rc == EXIT_OK
    assert len(calls["split"]) == 1
    call = calls["split"][0]
    # The segment_times_str must encode the 3 interior cut-points.
    assert call["segment_times_str"] == "1941.673,3883.346,5825.019"  # type: ignore[index]
    assert call["reencode"] is False  # type: ignore[index]


def test_run_equal_parts_returns_exit_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    _patch_dependencies(monkeypatch, tmp_path, duration_ms=10_000)
    args = _base_args(input=str(video), parts=2, yes=True)

    rc = run(args)

    assert rc == EXIT_OK


def test_run_ffmpeg_failure_returns_item_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    _patch_dependencies(monkeypatch, tmp_path, duration_ms=10_000, split_rc=1)
    args = _base_args(input=str(video), parts=2, yes=True)

    rc = run(args)

    assert rc == EXIT_ITEM_FAILED


# --- --yes flag skips questionary.confirm ---


def test_run_yes_flag_skips_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With --yes set, questionary.confirm must never be called."""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    _patch_dependencies(monkeypatch, tmp_path, duration_ms=10_000)

    import questionary as _q

    def boom_confirm(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            "questionary.confirm must not be called when --yes is set"
        )

    monkeypatch.setattr(_q, "confirm", boom_confirm)
    args = _base_args(input=str(video), parts=2, yes=True)

    rc = run(args)

    assert rc == EXIT_OK


# --- segment_time mode ---


def test_run_segment_time_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--segment-time 3.0 on a 10s video produces 3 interior points."""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    calls = _patch_dependencies(monkeypatch, tmp_path, duration_ms=10_000)
    args = _base_args(input=str(video), segment_time=3.0, yes=True)

    rc = run(args)

    assert rc == EXIT_OK
    assert len(calls["split"]) == 1
    # 10s / 3s → interior points at 3s, 6s, 9s.
    call = calls["split"][0]
    assert call["segment_times_str"] == "3.000,6.000,9.000"  # type: ignore[index]


# --- reencode flag ---


def test_run_reencode_flag_passed_to_split_video(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    calls = _patch_dependencies(monkeypatch, tmp_path, duration_ms=10_000)
    args = _base_args(input=str(video), parts=2, reencode=True, yes=True)

    rc = run(args)

    assert rc == EXIT_OK
    assert calls["split"][0]["reencode"] is True  # type: ignore[index]


# --- output_dir defaults to input parent ---


def test_run_default_output_dir_is_input_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    calls = _patch_dependencies(monkeypatch, tmp_path, duration_ms=10_000)
    args = _base_args(input=str(video), parts=2, yes=True)
    # output_dir is None → should default to video's parent.

    rc = run(args)

    assert rc == EXIT_OK
    call = calls["split"][0]
    # The output_pattern must be rooted in tmp_path (the input file's parent).
    assert str(tmp_path) in call["output_pattern"]  # type: ignore[index]


def test_run_custom_output_dir_is_respected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    out_dir = tmp_path / "parts"
    calls = _patch_dependencies(monkeypatch, tmp_path, duration_ms=10_000)
    args = _base_args(
        input=str(video),
        parts=2,
        output_dir=str(out_dir),
        yes=True,
    )

    rc = run(args)

    assert rc == EXIT_OK
    call = calls["split"][0]
    assert str(out_dir) in call["output_pattern"]  # type: ignore[index]


# ---------------------------------------------------------------------------
# interactive_args — mode/value prompts (new behaviour)
# ---------------------------------------------------------------------------


class _FakeQuestion:
    """Minimal stand-in for a questionary Question object (has .ask())."""

    def __init__(self, return_value: object) -> None:
        self._return_value = return_value

    def ask(self) -> object:
        return self._return_value


def _patch_questionary_interactive(
    monkeypatch: pytest.MonkeyPatch,
    *,
    input_value: str,
    mode_label: str,
    value_str: str,
    method_label: str,
) -> None:
    """Patch questionary inside split_module so interactive_args returns promptly.

    Call sequence in interactive_args (input=None path):
      1. questionary.path("Input video file:").ask()      → input_value
      2. questionary.select("Split into:", ...).ask()      → mode_label
      3. questionary.text("Number of parts:" or "Chunk length in seconds:", ...).ask() → value_str
      4. questionary.select("Split method:", ...).ask()    → method_label
      5. questionary.confirm("Proceed with the split?").ask() → True  (via run())
    """
    import questionary as _q

    _select_answers = [mode_label, method_label]
    _select_idx = [0]

    def fake_path(*_args: object, **_kwargs: object) -> _FakeQuestion:
        return _FakeQuestion(input_value)

    def fake_select(*_args: object, **_kwargs: object) -> _FakeQuestion:
        answer = _select_answers[_select_idx[0]]
        _select_idx[0] += 1
        return _FakeQuestion(answer)

    def fake_text(*_args: object, **_kwargs: object) -> _FakeQuestion:
        return _FakeQuestion(value_str)

    def fake_confirm(*_args: object, **_kwargs: object) -> _FakeQuestion:
        return _FakeQuestion(True)

    monkeypatch.setattr(_q, "path", fake_path)
    monkeypatch.setattr(_q, "select", fake_select)
    monkeypatch.setattr(_q, "text", fake_text)
    monkeypatch.setattr(_q, "confirm", fake_confirm)


def test_interactive_equal_parts_sets_parts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full interactive flow: selecting Equal parts + entering '4' sets parts=4."""
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"")
    calls = _patch_dependencies(monkeypatch, tmp_path, duration_ms=7_766_692)
    _patch_questionary_interactive(
        monkeypatch,
        input_value=str(video),
        mode_label=_MODE_EQUAL_LABEL,
        value_str="4",
        method_label=_METHOD_STREAM_COPY_LABEL,
    )

    # Pass a bare Namespace with all None — simulates zero CLI flags.
    args = argparse.Namespace(
        input=None,
        output_dir=None,
        parts=None,
        segment_time=None,
        reencode=None,
        yes=False,
        quiet=False,
    )
    rc = run(args)

    assert rc == EXIT_OK
    assert len(calls["split"]) == 1
    # Equal parts=4 on 7766692ms → 3 interior cut-points.
    assert calls["split"][0]["segment_times_str"] == "1941.673,3883.346,5825.019"  # type: ignore[index]
    assert calls["split"][0]["reencode"] is False  # type: ignore[index]


def test_interactive_fixed_chunks_sets_segment_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full interactive flow: selecting Fixed-length chunks + entering '600' sets segment_time=600.0."""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    calls = _patch_dependencies(monkeypatch, tmp_path, duration_ms=1_800_000)  # 1800s
    _patch_questionary_interactive(
        monkeypatch,
        input_value=str(video),
        mode_label=_MODE_SEGMENT_LABEL,
        value_str="600",
        method_label=_METHOD_STREAM_COPY_LABEL,
    )

    args = argparse.Namespace(
        input=None,
        output_dir=None,
        parts=None,
        segment_time=None,
        reencode=None,
        yes=False,
        quiet=False,
    )
    rc = run(args)

    assert rc == EXIT_OK
    assert len(calls["split"]) == 1
    # 1800s / 600s → 2 interior cut-points at 600s, 1200s.
    assert calls["split"][0]["segment_times_str"] == "600.000,1200.000"  # type: ignore[index]


def test_interactive_reencode_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting Re-encode method passes reencode=True to split_video."""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    calls = _patch_dependencies(monkeypatch, tmp_path, duration_ms=10_000)
    _patch_questionary_interactive(
        monkeypatch,
        input_value=str(video),
        mode_label=_MODE_EQUAL_LABEL,
        value_str="2",
        method_label=_METHOD_REENCODE_LABEL,
    )

    args = argparse.Namespace(
        input=None,
        output_dir=None,
        parts=None,
        segment_time=None,
        reencode=None,
        yes=False,
        quiet=False,
    )
    rc = run(args)

    assert rc == EXIT_OK
    assert calls["split"][0]["reencode"] is True  # type: ignore[index]



def test_interactive_validate_parts_rejects_abc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Number-of-parts validator rejects 'abc' and returns an error string."""
    import questionary as _q

    captured_validators: list[object] = []

    def fake_path(*_a: object, **_kw: object) -> object:
        class _R:
            def ask(self) -> str:
                return "/fake/video.mp4"
        return _R()

    _select_call_idx = [0]
    _select_answers_list = [_MODE_EQUAL_LABEL]

    def fake_select(*_a: object, **_kw: object) -> object:
        ans = _select_answers_list[_select_call_idx[0] % len(_select_answers_list)]
        _select_call_idx[0] += 1
        class _R:
            def ask(self_inner) -> str:  # noqa: N805
                return ans
        return _R()

    def fake_text(*_a: object, **kw: object) -> object:
        if "validate" in kw:
            captured_validators.append(kw["validate"])
        class _R:
            def ask(self) -> str:
                return "3"
        return _R()

    monkeypatch.setattr(_q, "path", fake_path)
    monkeypatch.setattr(_q, "select", fake_select)
    monkeypatch.setattr(_q, "text", fake_text)

    from media_toolkit.videos.split import interactive_args

    prefilled = argparse.Namespace(
        input=None,
        output_dir=None,
        parts=None,
        segment_time=None,
        reencode=None,
        yes=False,
        quiet=False,
    )
    interactive_args(prefilled=prefilled)

    assert len(captured_validators) == 1, "Expected exactly one text validator (for parts)"
    validate = captured_validators[0]
    assert callable(validate)
    # "abc" must be rejected.
    result = validate("abc")  # type: ignore[operator]
    assert result is not True, f"Expected rejection of 'abc', got: {result!r}"
    # A valid int >= MIN_PARTS must be accepted.
    assert validate("3") is True  # type: ignore[operator]
    # An int < MIN_PARTS must be rejected.
    result_low = validate("1")  # type: ignore[operator]
    assert result_low is not True, f"Expected rejection of '1' (< MIN_PARTS={MIN_PARTS})"


def test_interactive_validate_segment_rejects_abc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Chunk-length validator rejects 'abc' and values <= 0."""
    import questionary as _q

    captured_validators: list[object] = []

    def fake_path(*_a: object, **_kw: object) -> object:
        class _R:
            def ask(self) -> str:
                return "/fake/video.mp4"
        return _R()

    _select_call_idx = [0]
    _select_answers_list = [_MODE_SEGMENT_LABEL]

    def fake_select(*_a: object, **_kw: object) -> object:
        ans = _select_answers_list[_select_call_idx[0] % len(_select_answers_list)]
        _select_call_idx[0] += 1
        class _R:
            def ask(self_inner) -> str:  # noqa: N805
                return ans
        return _R()

    def fake_text(*_a: object, **kw: object) -> object:
        if "validate" in kw:
            captured_validators.append(kw["validate"])
        class _R:
            def ask(self) -> str:
                return "30.0"
        return _R()

    monkeypatch.setattr(_q, "path", fake_path)
    monkeypatch.setattr(_q, "select", fake_select)
    monkeypatch.setattr(_q, "text", fake_text)

    from media_toolkit.videos.split import interactive_args

    prefilled = argparse.Namespace(
        input=None,
        output_dir=None,
        parts=None,
        segment_time=None,
        reencode=None,
        yes=False,
        quiet=False,
    )
    interactive_args(prefilled=prefilled)

    assert len(captured_validators) == 1, "Expected exactly one text validator (for segment)"
    validate = captured_validators[0]
    assert callable(validate)
    # "abc" must be rejected.
    result = validate("abc")  # type: ignore[operator]
    assert result is not True, f"Expected rejection of 'abc', got: {result!r}"
    # Zero must be rejected.
    result_zero = validate("0")  # type: ignore[operator]
    assert result_zero is not True, f"Expected rejection of '0'"
    # Negative must be rejected.
    result_neg = validate("-5")  # type: ignore[operator]
    assert result_neg is not True, f"Expected rejection of '-5'"
    # A valid positive float must be accepted.
    assert validate("30.5") is True  # type: ignore[operator]


def test_interactive_abort_on_mode_select_returns_user_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user aborts the 'Split into:' prompt (returns None), run() returns EXIT_USER_ABORT."""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    _patch_dependencies(monkeypatch, tmp_path, duration_ms=10_000)

    import questionary as _q

    def fake_path(*_a: object, **_kw: object) -> object:
        class _R:
            def ask(self) -> str:
                return str(video)
        return _R()

    def fake_select_abort(*_a: object, **_kw: object) -> object:
        class _R:
            def ask(self) -> None:
                return None  # user aborted
        return _R()

    monkeypatch.setattr(_q, "path", fake_path)
    monkeypatch.setattr(_q, "select", fake_select_abort)

    args = argparse.Namespace(
        input=None,
        output_dir=None,
        parts=None,
        segment_time=None,
        reencode=None,
        yes=False,
        quiet=False,
    )
    rc = run(args)

    assert rc == EXIT_USER_ABORT
