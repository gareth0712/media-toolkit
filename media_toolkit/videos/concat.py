"""Concatenate sequential MP4s in a chapter directory and merge their SRTs.

The op scans a chapter directory for files following the naming convention
``<num1>-<num2> <rest>.mp4`` (and matching ``.<lang>.srt`` siblings), groups
them into sections sorted in natural numeric order, then produces:

* A single merged MP4 (stream-copy first; optional libx264 re-encode fallback).
* One merged SRT per language observed across the sections, with cue
  timestamps shifted by the cumulative duration of preceding videos.
* A ``timestamps.txt`` file in YouTube chapter format.

Pure logic helpers (filename parsing, sort key, timestamp formatting, SRT
merging, concat list construction) are factored out so they can be tested
without ffmpeg/ffprobe being available.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pysrt

from media_toolkit.path_utils import normalize_path_input

logger = logging.getLogger(__name__)

NAME = "concat"
DESCRIPTION = (
    "Merge sequential MP4s in a chapter folder, shift SRT timestamps, "
    "and write a YouTube-style timestamps.txt"
)

# Module-level constants (no magic strings/numbers in business logic).
LANGS_ORDERED: tuple[str, ...] = ("zh-TW", "tw", "ja", "en-x-autogen")
SECTION_PATTERN = re.compile(r"^(\d+)-(\d+)\s+(.+)$")
SRT_SUFFIX = ".srt"
MP4_SUFFIX = ".mp4"
TIMESTAMPS_FILENAME = "timestamps.txt"

# ffmpeg re-encode parameters used when stream-copy fails and the user opted
# in via --reencode-on-failure.
REENCODE_VIDEO_CODEC = "libx264"
REENCODE_VIDEO_PRESET = "medium"
REENCODE_VIDEO_CRF = "20"
REENCODE_AUDIO_CODEC = "aac"
REENCODE_AUDIO_BITRATE = "128k"

# ffmpeg / ffprobe executables. Resolved via PATH at call time.
#
# Snap-installed ffmpeg on Ubuntu exposes ffprobe as ``ffmpeg.ffprobe`` (note
# the dot) rather than the standard ``ffprobe`` name. We probe the candidate
# tuples in order via ``shutil.which`` so the toolkit works on either layout
# without requiring environment overrides.
FFMPEG_CANDIDATES: tuple[str, ...] = ("ffmpeg",)
FFPROBE_CANDIDATES: tuple[str, ...] = ("ffprobe", "ffmpeg.ffprobe")

# Cached resolved binary names. Populated lazily by ``_ensure_dependencies``;
# every subprocess call goes through ``_get_ffmpeg_bin`` / ``_get_ffprobe_bin``
# so we never hardcode a name that might not exist on a given host.
_RESOLVED_FFMPEG_BIN: str | None = None
_RESOLVED_FFPROBE_BIN: str | None = None

# Exit codes (mirrors HANDOFF Section 7).
EXIT_OK = 0
EXIT_ITEM_FAILED = 1
EXIT_SETUP_ERROR = 2

# Conversion factor for the duration string emitted by ffprobe (seconds).
SECONDS_TO_MS = 1000

# Tolerances for "merged duration matches sum of inputs" check after a concat.
#
# stream-copy preserves frames exactly so any meaningful gap between expected
# and actual duration indicates ffmpeg silently dropped one or more inputs
# (classic symptom of inconsistent fps/SAR across sources — concat demuxer
# can return rc=0 yet stop reading at the first non-monotonic timestamp).
# A 2-second tolerance covers normal frame-boundary rounding.
STREAM_COPY_TOLERANCE_MS = 2000

# Re-encode rebuilds the timestamp track so it can drift slightly more (the
# encoder may pad the final GOP, drop a duplicate frame, etc.). Five seconds
# is generous enough to never false-positive while still catching anything
# that looks like a real truncation.
REENCODE_TOLERANCE_MS = 5000


class ConcatError(Exception):
    """Base error for the concat op."""


class DuplicateSectionError(ConcatError):
    """Raised when two distinct mp4 files share the same (num1, num2) key."""


class MissingDependencyError(ConcatError):
    """Raised when ffmpeg / ffprobe are not on PATH."""


class StreamCopyFailedError(ConcatError):
    """Raised when ffmpeg ``-c copy`` fails and re-encode is not enabled."""


@dataclass(frozen=True)
class Section:
    """One section worth of files: an mp4 and zero or more language SRTs."""

    key: tuple[int, int]
    label: str
    mp4_path: Path
    srts: dict[str, Path] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure logic (no ffmpeg / ffprobe required; tested directly).
# ---------------------------------------------------------------------------


def section_sort_key(filename: str) -> tuple[int, int]:
    """Return the (num1, num2) tuple parsed from a section filename.

    Raises ValueError if the filename does not start with ``<int>-<int> ``.
    """
    match = SECTION_PATTERN.match(filename)
    if match is None:
        raise ValueError(f"filename does not match section pattern: {filename!r}")
    return (int(match.group(1)), int(match.group(2)))


def format_timestamp(total_seconds: float) -> str:
    """Format a duration in seconds as zero-padded ``HH:MM:SS``."""
    whole = int(total_seconds)
    hours, rem = divmod(whole, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _parse_section_basename(stem: str) -> tuple[tuple[int, int], str] | None:
    """Parse an mp4 stem into (key, label). Returns None if not a section file."""
    match = SECTION_PATTERN.match(stem)
    if match is None:
        return None
    key = (int(match.group(1)), int(match.group(2)))
    return key, stem  # label is the full stem (keeps suffix like __解惑)


def _split_srt_lang(srt_name: str) -> tuple[str, str] | None:
    """Split an SRT filename into (mp4_stem, lang). Returns None if no lang."""
    if not srt_name.endswith(SRT_SUFFIX):
        return None
    without_ext = srt_name[: -len(SRT_SUFFIX)]
    if "." not in without_ext:
        return None
    mp4_stem, lang = without_ext.rsplit(".", 1)
    return mp4_stem, lang


def discover_sections(input_dir: Path) -> list[Section]:
    """Scan ``input_dir`` and return a sorted list of Section objects.

    Raises:
        FileNotFoundError: if ``input_dir`` does not exist.
        DuplicateSectionError: if two distinct mp4s share the same key.
    """
    if not input_dir.is_dir():
        raise FileNotFoundError(f"input directory does not exist: {input_dir}")

    # Collect every mp4 per section key, then detect duplicates after the
    # full scan so the error message can list ALL colliding files (not just
    # the second-and-later ones).
    mp4s_by_key: dict[tuple[int, int], list[Path]] = {}

    for entry in sorted(input_dir.iterdir()):
        if not entry.is_file() or entry.suffix.lower() != MP4_SUFFIX:
            continue
        parsed = _parse_section_basename(entry.stem)
        if parsed is None:
            continue
        key, _label = parsed
        mp4s_by_key.setdefault(key, []).append(entry)

    duplicates = {key: paths for key, paths in mp4s_by_key.items() if len(paths) > 1}
    if duplicates:
        lines = ["duplicate sections detected (same num1-num2):"]
        for key, paths in sorted(duplicates.items()):
            lines.append(f"  {key[0]}-{key[1]}:")
            for path in paths:
                lines.append(f"    {path}")
        raise DuplicateSectionError("\n".join(lines))

    mp4_by_key: dict[tuple[int, int], Path] = {
        key: paths[0] for key, paths in mp4s_by_key.items()
    }

    # Index SRTs by mp4 stem so we can attach them to their section.
    srts_by_stem: dict[str, dict[str, Path]] = {}
    for entry in input_dir.iterdir():
        if not entry.is_file() or entry.suffix.lower() != SRT_SUFFIX:
            continue
        split = _split_srt_lang(entry.name)
        if split is None:
            continue
        mp4_stem, lang = split
        srts_by_stem.setdefault(mp4_stem, {})[lang] = entry

    sections: list[Section] = []
    for key in sorted(mp4_by_key):
        mp4_path = mp4_by_key[key]
        label = mp4_path.stem
        srts = srts_by_stem.get(label, {})
        sections.append(Section(key=key, label=label, mp4_path=mp4_path, srts=dict(srts)))
    return sections


def build_concat_list_text(mp4_paths: Iterable[Path]) -> str:
    """Build the text content of an ffmpeg concat-demuxer list file.

    Each entry is ``file '<absolute path>'`` with embedded single quotes
    escaped by doubling.
    """
    lines: list[str] = []
    for path in mp4_paths:
        absolute = str(path.resolve())
        escaped = absolute.replace("'", "''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


def merge_srts(
    srt_paths: list[Path | None],
    durations_ms: list[int],
    output_path: Path,
) -> None:
    """Merge a parallel list of SRT files into one, shifting by cumulative offsets.

    ``srt_paths[i]`` may be ``None`` to indicate the language is missing for
    that section; the offset still advances based on ``durations_ms[i]``.
    Cues are renumbered starting at 1 in the merged output.
    """
    if len(srt_paths) != len(durations_ms):
        raise ValueError(
            f"srt_paths and durations_ms length mismatch: "
            f"{len(srt_paths)} vs {len(durations_ms)}"
        )

    merged = pysrt.SubRipFile()
    offset_ms = 0
    for srt_path, duration_ms in zip(srt_paths, durations_ms):
        if srt_path is not None:
            subs = pysrt.open(str(srt_path), encoding="utf-8")
            subs.shift(milliseconds=offset_ms)
            merged.extend(subs)
        offset_ms += duration_ms

    for new_index, sub in enumerate(merged, start=1):
        sub.index = new_index

    merged.save(str(output_path), encoding="utf-8")


def build_timestamps_text(sections: list[Section], durations_ms: list[int]) -> str:
    """Build YouTube-style chapter timestamps text, one line per section.

    First line is always ``00:00:00``; each subsequent line uses the cumulative
    duration of all preceding sections (in milliseconds, truncated to seconds).
    """
    if len(sections) != len(durations_ms):
        raise ValueError(
            f"sections and durations_ms length mismatch: "
            f"{len(sections)} vs {len(durations_ms)}"
        )

    lines: list[str] = []
    cumulative_ms = 0
    for section, duration_ms in zip(sections, durations_ms):
        timestamp = format_timestamp(cumulative_ms / SECONDS_TO_MS)
        lines.append(f"{timestamp} {section.label}")
        cumulative_ms += duration_ms
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Thin ffmpeg / ffprobe wrappers (isolated for mocking).
# ---------------------------------------------------------------------------


def _resolve_binary(candidates: tuple[str, ...]) -> str:
    """Return the first executable in ``candidates`` resolvable on PATH.

    Raises:
        MissingDependencyError: if none of the candidates resolve.
    """
    for name in candidates:
        if shutil.which(name) is not None:
            return name
    raise MissingDependencyError(
        f"none of these executables found on PATH: {', '.join(candidates)}"
    )


def _ensure_dependencies() -> None:
    """Resolve ffmpeg/ffprobe binaries and cache them at module level.

    Raises:
        MissingDependencyError: if either binary cannot be resolved. The
            exception message lists every candidate name that was tried so the
            user can see exactly what's expected on PATH.
    """
    global _RESOLVED_FFMPEG_BIN, _RESOLVED_FFPROBE_BIN
    _RESOLVED_FFMPEG_BIN = _resolve_binary(FFMPEG_CANDIDATES)
    _RESOLVED_FFPROBE_BIN = _resolve_binary(FFPROBE_CANDIDATES)


def _get_ffmpeg_bin() -> str:
    """Return the resolved ffmpeg binary name (auto-resolves if needed)."""
    if _RESOLVED_FFMPEG_BIN is None:
        _ensure_dependencies()
    # _ensure_dependencies populates the cache or raises; assert for type narrowing.
    assert _RESOLVED_FFMPEG_BIN is not None
    return _RESOLVED_FFMPEG_BIN


def _get_ffprobe_bin() -> str:
    """Return the resolved ffprobe binary name (auto-resolves if needed)."""
    if _RESOLVED_FFPROBE_BIN is None:
        _ensure_dependencies()
    assert _RESOLVED_FFPROBE_BIN is not None
    return _RESOLVED_FFPROBE_BIN


def get_duration_ms(mp4_path: Path) -> int:
    """Return the duration of an mp4 file in milliseconds via ffprobe."""
    result = subprocess.run(
        [
            _get_ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(mp4_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ConcatError(
            f"ffprobe failed for {mp4_path}: {result.stderr.strip()}"
        )
    raw = result.stdout.strip()
    if not raw:
        raise ConcatError(f"ffprobe returned empty duration for {mp4_path}")
    return int(float(raw) * SECONDS_TO_MS)


# Workdir for intermediate MPEG-TS segments. Lives next to the merged output
# (snap-confined ffmpeg can't read /tmp). Cleaned up in the ``finally`` block
# of ``concat_videos``.
TS_WORKDIR_NAME = ".concat-work"
TS_SEGMENT_PREFIX = "seg_"
TS_SEGMENT_SUFFIX = ".ts"


def _convert_source_to_ts(src: Path, dest_ts: Path, reencode: bool) -> int:
    """Convert one source MP4 to an MPEG-TS segment via ffmpeg.

    When ``reencode`` is False, uses stream-copy with the
    ``h264_mp4toannexb`` bitstream filter (required to translate MP4's avc1
    NAL format into TS's annex-B). When True, transcodes to libx264 + aac;
    libx264 emits annex-B natively so no bsf is needed.

    Returns the ffmpeg process return code. ``-nostdin`` is set so ffmpeg
    won't consume the parent process's stdin in heredoc/piped contexts.
    """
    if reencode:
        codec_args = [
            "-c:v",
            REENCODE_VIDEO_CODEC,
            "-preset",
            REENCODE_VIDEO_PRESET,
            "-crf",
            REENCODE_VIDEO_CRF,
            "-c:a",
            REENCODE_AUDIO_CODEC,
            "-b:a",
            REENCODE_AUDIO_BITRATE,
        ]
    else:
        # Stream-copy: bsf converts AVC (length-prefixed NALs) to annex-B
        # (start-code-prefixed NALs) which is what mpegts requires.
        codec_args = ["-c", "copy", "-bsf:v", "h264_mp4toannexb"]

    cmd = [
        _get_ffmpeg_bin(),
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(src),
        *codec_args,
        "-f",
        "mpegts",
        str(dest_ts),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(
            "ffmpeg TS convert (reencode=%s) failed for %s: rc=%d; stderr:\n%s",
            reencode,
            src,
            result.returncode,
            result.stderr.strip(),
        )
    return result.returncode


def _run_ts_concat(ts_segments: list[Path], output_path: Path) -> int:
    """Concatenate MPEG-TS segments into a final MP4 via the concat protocol.

    Uses ``concat:`` URL syntax (NOT the concat demuxer). The concat
    protocol is byte-level for raw container formats like TS, which avoids
    the demuxer's PTS-accumulation issues that silently truncate output
    when source fps differ. ``-bsf:a aac_adtstoasc`` converts AAC ADTS
    frames (TS) back to ASC (MP4); this is a no-op for non-AAC audio.

    Returns the ffmpeg process return code.
    """
    concat_arg = "concat:" + "|".join(str(seg) for seg in ts_segments)
    cmd = [
        _get_ffmpeg_bin(),
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        concat_arg,
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        str(output_path),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(
            "ffmpeg TS concat returned rc=%d; stderr:\n%s",
            result.returncode,
            result.stderr.strip(),
        )
    return result.returncode


def _ts_concat_pass(
    mp4_paths: list[Path],
    output_path: Path,
    workdir: Path,
    reencode: bool,
) -> int:
    """Run one full TS-intermediate concat pass (convert + concat).

    Cleans any prior ``*.ts`` segments and prior output before starting so a
    re-encode retry doesn't pick up stale stream-copy artefacts. Returns
    the concat protocol's return code; conversion failures raise
    ``StreamCopyFailedError`` immediately because they should never happen
    on well-formed inputs.
    """
    # Clear stale segments (re-encode pass) and any prior output.
    for stale in workdir.glob(f"*{TS_SEGMENT_SUFFIX}"):
        try:
            stale.unlink()
        except OSError:
            pass
    try:
        output_path.unlink()
    except FileNotFoundError:
        pass

    ts_segments: list[Path] = []
    for index, src in enumerate(mp4_paths):
        dest_ts = workdir / f"{TS_SEGMENT_PREFIX}{index}{TS_SEGMENT_SUFFIX}"
        rc = _convert_source_to_ts(src, dest_ts, reencode=reencode)
        if rc != 0:
            raise StreamCopyFailedError(
                f"ffmpeg failed to convert {src} to MPEG-TS "
                f"(reencode={reencode}, rc={rc})"
            )
        ts_segments.append(dest_ts)

    return _run_ts_concat(ts_segments, output_path)


def concat_videos(
    mp4_paths: list[Path],
    output_path: Path,
    source_durations_ms: list[int],
    reencode_on_failure: bool,
) -> None:
    """Concatenate ``mp4_paths`` into ``output_path`` via TS intermediates.

    Each source is first converted to an MPEG-TS segment (stream-copy with
    ``h264_mp4toannexb`` bsf). The segments are then joined via ffmpeg's
    concat protocol — a byte-level concatenation that avoids the concat
    demuxer's PTS-accumulation truncation when source fps differ.

    Verifies the merged file's actual duration matches the sum of source
    durations (within ``STREAM_COPY_TOLERANCE_MS``). If either the concat
    step's rc is non-zero or the duration check fails, falls back to
    libx264 + aac re-encode when ``reencode_on_failure`` is set; otherwise
    raises ``StreamCopyFailedError``.

    ``source_durations_ms`` must be parallel to ``mp4_paths``: each entry is
    the ffprobe-reported duration of the corresponding input. Threading this
    through from the caller avoids re-probing the same files (the caller
    already needs these durations for SRT shifting and timestamps.txt).
    """
    if len(mp4_paths) != len(source_durations_ms):
        raise ValueError(
            f"mp4_paths and source_durations_ms length mismatch: "
            f"{len(mp4_paths)} vs {len(source_durations_ms)}"
        )

    expected_ms = sum(source_durations_ms)

    # Workdir lives next to the output: the same directory ffmpeg can write
    # the final merged mp4 into is by definition reachable from the snap
    # sandbox, so /tmp issues don't apply here.
    workdir = output_path.parent / TS_WORKDIR_NAME
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        copy_rc = _ts_concat_pass(
            mp4_paths, output_path, workdir, reencode=False
        )
        copy_truncation_msg: str | None = None
        if copy_rc == 0:
            actual_ms = get_duration_ms(output_path)
            delta_ms = abs(actual_ms - expected_ms)
            if delta_ms <= STREAM_COPY_TOLERANCE_MS:
                return  # success
            copy_truncation_msg = (
                f"stream-copy TS concat returned 0 but merged duration "
                f"{actual_ms} ms differs from expected {expected_ms} ms by "
                f"{delta_ms} ms — likely silent truncation (inconsistent "
                f"codec params across sources)"
            )
            logger.warning("%s", copy_truncation_msg)

        if not reencode_on_failure:
            if copy_truncation_msg is not None:
                raise StreamCopyFailedError(
                    copy_truncation_msg
                    + "; re-run with --reencode-on-failure to fall back to "
                    "libx264/aac"
                )
            raise StreamCopyFailedError(
                f"ffmpeg TS concat failed (rc={copy_rc}); re-run with "
                f"--reencode-on-failure to fall back to libx264/aac"
            )

        logger.info(
            "retrying concat with re-encode (libx264/aac) — this is slow "
            "(~10-20 min/chapter)..."
        )
        reencode_rc = _ts_concat_pass(
            mp4_paths, output_path, workdir, reencode=True
        )
        if reencode_rc != 0:
            raise StreamCopyFailedError(
                f"ffmpeg re-encode TS concat also failed (rc={reencode_rc})"
            )
        actual_ms = get_duration_ms(output_path)
        delta_ms = abs(actual_ms - expected_ms)
        if delta_ms > REENCODE_TOLERANCE_MS:
            raise StreamCopyFailedError(
                f"re-encode produced wrong duration "
                f"({actual_ms} ms vs expected {expected_ms} ms, "
                f"delta {delta_ms} ms)"
            )
    finally:
        # Always clean up the workdir, even on error: leftover ~hundreds of
        # MB of TS segments per chapter add up fast.
        try:
            shutil.rmtree(workdir)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CLI integration.
# ---------------------------------------------------------------------------


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the concat subcommand under the videos domain parser.

    All args default to ``None`` so callers can distinguish "user did not
    supply this" from a real value; ``interactive_args`` then prompts only
    for the missing fields.
    """
    parser = subparsers.add_parser(NAME, help=DESCRIPTION)
    parser.add_argument(
        "--input-dir",
        required=False,
        default=None,
        help="Chapter directory containing the section mp4s and srts.",
    )
    parser.add_argument(
        "--output-dir",
        required=False,
        default=None,
        help="Destination directory; a subfolder named after the chapter is created.",
    )
    parser.add_argument(
        "--reencode-on-failure",
        action="store_true",
        # default=None acts as a sentinel meaning "user did not specify";
        # store_true still flips it to True when present.
        default=None,
        help="Fall back to libx264/aac re-encode if ffmpeg stream-copy fails.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress console progress output (file log is unaffected).",
    )
    parser.set_defaults(func=run)


def interactive_args(
    prefilled: argparse.Namespace | None = None,
) -> argparse.Namespace:
    """Fill any missing concat args via questionary prompts.

    For each field, the value from ``prefilled`` is kept if it is not
    ``None``; otherwise the user is prompted. Returns a fully populated
    ``argparse.Namespace``.

    Special-case: ``reencode_on_failure`` is only prompted when at least one
    of the required directory args is also missing. If both ``input_dir`` and
    ``output_dir`` are pre-supplied (scripted invocation), an unspecified
    ``reencode_on_failure`` defaults to ``False`` so the run proceeds without
    interactive prompts.

    If the user aborts a prompt (Ctrl-C), questionary returns ``None`` and
    that ``None`` is propagated up so the caller can detect the abort.
    """
    import questionary

    base = prefilled if prefilled is not None else argparse.Namespace()

    input_dir = getattr(base, "input_dir", None)
    output_dir = getattr(base, "output_dir", None)
    reencode = getattr(base, "reencode_on_failure", None)

    interactive_mode = input_dir is None or output_dir is None

    if input_dir is None:
        input_dir = questionary.path("Input directory:").ask()

    if output_dir is None:
        output_dir = questionary.path("Output directory:").ask()

    if reencode is None:
        if interactive_mode:
            reencode = questionary.confirm(
                "Re-encode if stream-copy fails?", default=True
            ).ask()
        else:
            reencode = False

    quiet = bool(getattr(base, "quiet", False))

    return argparse.Namespace(
        input_dir=input_dir,
        output_dir=output_dir,
        reencode_on_failure=bool(reencode) if reencode is not None else None,
        quiet=quiet,
    )


def run(args: argparse.Namespace) -> int:
    """Execute the concat op. Returns an exit code per HANDOFF Section 7."""
    # Fill any missing fields via questionary prompts.
    args = interactive_args(prefilled=args)

    # Detect user abort (Ctrl-C inside a questionary prompt yields None).
    if args.input_dir is None or args.output_dir is None or args.reencode_on_failure is None:
        logger.error("error: aborted by user")
        return EXIT_SETUP_ERROR

    input_dir = normalize_path_input(args.input_dir).expanduser().resolve()
    output_root = normalize_path_input(args.output_dir).expanduser().resolve()

    try:
        _ensure_dependencies()
    except MissingDependencyError as exc:
        logger.error("error: %s", exc)
        return EXIT_SETUP_ERROR

    try:
        sections = discover_sections(input_dir)
    except FileNotFoundError as exc:
        logger.error("error: %s", exc)
        return EXIT_SETUP_ERROR
    except DuplicateSectionError as exc:
        logger.error("error: %s", exc)
        return EXIT_SETUP_ERROR

    if not sections:
        logger.error("error: no section mp4 files found in %s", input_dir)
        return EXIT_SETUP_ERROR

    chapter_name = input_dir.name
    chapter_out_dir = output_root / chapter_name
    chapter_out_dir.mkdir(parents=True, exist_ok=True)

    total = len(sections)
    durations_ms: list[int] = []
    for index, section in enumerate(sections, start=1):
        logger.info("[%d/%d] probing %s", index, total, section.label)
        durations_ms.append(get_duration_ms(section.mp4_path))

    merged_mp4_path = chapter_out_dir / f"{chapter_name}{MP4_SUFFIX}"
    logger.info("concatenating %d mp4(s) -> %s", total, merged_mp4_path)
    try:
        concat_videos(
            [s.mp4_path for s in sections],
            merged_mp4_path,
            source_durations_ms=durations_ms,
            reencode_on_failure=bool(args.reencode_on_failure),
        )
    except (StreamCopyFailedError, ConcatError) as exc:
        logger.error("error: %s", exc)
        # Don't leave a half-written / wrong-duration mp4 sitting around —
        # downstream re-runs would otherwise pick it up as if it were valid.
        try:
            merged_mp4_path.unlink()
        except FileNotFoundError:
            pass
        return EXIT_ITEM_FAILED

    # Determine which languages have any srt at all; preserve preferred order
    # for known langs and append unknown langs alphabetically at the end.
    observed_langs: set[str] = set()
    for section in sections:
        observed_langs.update(section.srts.keys())
    ordered_langs = [lang for lang in LANGS_ORDERED if lang in observed_langs]
    extras = sorted(observed_langs - set(LANGS_ORDERED))
    ordered_langs.extend(extras)

    for lang in ordered_langs:
        srt_paths: list[Path | None] = [s.srts.get(lang) for s in sections]
        if all(path is None for path in srt_paths):
            continue
        merged_srt_path = chapter_out_dir / f"{chapter_name}.{lang}{SRT_SUFFIX}"
        logger.info("merging srt (%s) -> %s", lang, merged_srt_path)
        merge_srts(srt_paths, durations_ms, merged_srt_path)

    timestamps_text = build_timestamps_text(sections, durations_ms)
    timestamps_path = chapter_out_dir / TIMESTAMPS_FILENAME
    logger.info("writing %s", timestamps_path)
    timestamps_path.write_text(timestamps_text, encoding="utf-8")

    return EXIT_OK
