"""Split a single video into multiple parts via the ffmpeg segment muxer.

Supports two modes:

* **Equal parts** (``--parts N``): divides the video into N segments of
  approximately equal duration. Cut points are calculated as
  ``round(duration_ms * k / N)`` for ``k = 1 .. N-1``. Cuts snap to the
  nearest keyframe when stream-copying.
* **Fixed-length chunks** (``--segment-time S``): emits chunks of S seconds
  each; the last chunk may be shorter than S. The two modes are mutually
  exclusive.

Default method is **stream copy** (``-c copy``, lossless, fast, keyframe-
snapped). Pass ``--reencode`` to instead re-encode with libx264/aac for
frame-exact cut boundaries (slow).

Pure-logic helpers (``compute_equal_split_points``,
``compute_segment_time_points``, ``format_segment_times``,
``build_output_pattern``, ``expected_part_count``) are unit-testable without
ffmpeg/ffprobe being installed. The thin ffmpeg wrapper (``split_video``) is
isolated for monkeypatching.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

from media_toolkit._ffmpeg import (
    MissingDependencyError,
    get_duration_ms,
    resolve_ffmpeg_bin,
    resolve_ffprobe_bin,
)
from media_toolkit.path_utils import normalize_path_input

logger = logging.getLogger(__name__)

NAME = "split"
DESCRIPTION = "Split a video into N equal parts or fixed-length chunks (stream copy or re-encode)."

# Exit codes (mirrors HANDOFF Section 7).
EXIT_OK = 0
EXIT_ITEM_FAILED = 1
EXIT_SETUP_ERROR = 2
EXIT_USER_ABORT = 130

# Re-encode codec constants (mirrors concat.py for consistency).
REENCODE_VIDEO_CODEC = "libx264"
REENCODE_VIDEO_PRESET = "medium"
REENCODE_VIDEO_CRF = "20"
REENCODE_AUDIO_CODEC = "aac"
REENCODE_AUDIO_BITRATE = "128k"

# Conversion factor for ffprobe duration (seconds) → milliseconds.
SECONDS_TO_MS = 1000

# Minimum number of parts for equal-split mode.
MIN_PARTS = 2

# Interactive-prompt label constants.
_METHOD_STREAM_COPY_LABEL = "Stream copy (lossless, fast, keyframe-snapped)"
_METHOD_REENCODE_LABEL = "Re-encode (libx264/aac, exact-length parts, slow)"


# ---------------------------------------------------------------------------
# Domain exception.
# ---------------------------------------------------------------------------


class SplitError(Exception):
    """Base error for the split op."""


# ---------------------------------------------------------------------------
# Immutable plan dataclass.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SplitPlan:
    """Fully-resolved parameters for a single split job."""

    input_path: Path
    output_dir: Path
    points_ms: list[int]
    reencode: bool
    mode_label: str  # human-readable description for the preview


# ---------------------------------------------------------------------------
# Pure logic (no ffmpeg / ffprobe required; unit-testable directly).
# ---------------------------------------------------------------------------


def compute_equal_split_points(duration_ms: int, parts: int) -> list[int]:
    """Return the ``parts-1`` interior cut-points (in ms) for equal-part splitting.

    Cut point ``k`` = ``round(duration_ms * k / parts)`` for ``k = 1 .. parts-1``.

    Args:
        duration_ms: Total video duration in milliseconds. Must be > 0.
        parts: Number of equal parts to produce. Must be >= 2.

    Returns:
        List of ``parts - 1`` cut points in ms, strictly between 0 and
        ``duration_ms``.

    Raises:
        ValueError: if ``parts < 2`` or ``duration_ms <= 0``.
    """
    if parts < MIN_PARTS:
        raise ValueError(
            f"--parts must be at least {MIN_PARTS}; got {parts}"
        )
    if duration_ms <= 0:
        raise ValueError(
            f"duration_ms must be positive; got {duration_ms}"
        )
    return [round(duration_ms * k / parts) for k in range(1, parts)]


def compute_segment_time_points(duration_ms: int, segment_ms: int) -> list[int]:
    """Return interior cut-points for fixed-length chunk splitting.

    Points are at ``segment_ms``, ``2*segment_ms``, … strictly less than
    ``duration_ms`` (the last chunk may be shorter than ``segment_ms``).

    Args:
        duration_ms: Total video duration in milliseconds. Must be > 0.
        segment_ms: Desired chunk length in milliseconds. Must be > 0 and
            < ``duration_ms`` (otherwise the result would be a single part,
            which is not a meaningful split).

    Returns:
        List of interior cut points. May be empty only if the segment covers
        the entire video — but that case is rejected by the validation below.

    Raises:
        ValueError: if ``segment_ms <= 0``, or ``segment_ms >= duration_ms``.
    """
    if segment_ms <= 0:
        raise ValueError(
            f"--segment-time must be positive; got {segment_ms / SECONDS_TO_MS:.3f}s"
        )
    if segment_ms >= duration_ms:
        raise ValueError(
            f"--segment-time ({segment_ms / SECONDS_TO_MS:.3f}s) must be shorter than "
            f"the video duration ({duration_ms / SECONDS_TO_MS:.3f}s); "
            "it would produce a single part, which is not a split"
        )
    points: list[int] = []
    cursor = segment_ms
    while cursor < duration_ms:
        points.append(cursor)
        cursor += segment_ms
    return points


def format_segment_times(points_ms: list[int]) -> str:
    """Convert a list of millisecond cut-points to the ffmpeg ``-segment_times`` string.

    Each point is converted to seconds with 3 decimal places, then joined by
    commas — e.g. ``[1941673, 3883346]`` → ``"1941.673,3883.346"``.

    Args:
        points_ms: Interior cut points in milliseconds (may be empty).

    Returns:
        Comma-joined seconds string, or ``""`` for an empty list.
    """
    return ",".join(f"{ms / SECONDS_TO_MS:.3f}" for ms in points_ms)


def build_output_pattern(input_path: Path, output_dir: Path) -> str:
    """Build the ffmpeg printf-style output pattern for segment files.

    The pattern is ``<output_dir>/<stem>_part%d<suffix>`` where ``stem`` and
    ``suffix`` are taken from ``input_path``. The ``%d`` placeholder is filled
    by ffmpeg's segment muxer with the part number.

    Args:
        input_path: Source video file. Its stem and suffix are used.
        output_dir: Directory where parts will be written.

    Returns:
        Absolute path string suitable for passing to ffmpeg as the output
        argument (e.g. ``"/out/video_part%d.mp4"``).
    """
    stem = input_path.stem
    suffix = input_path.suffix
    return str(output_dir / f"{stem}_part%d{suffix}")


def expected_part_count(points_ms: list[int]) -> int:
    """Return the number of output segments given a list of interior cut-points.

    N cut-points produce N+1 segments.

    Args:
        points_ms: Interior cut points (may be empty for a zero-split degenerate
            case, though ``compute_*`` helpers prevent that in practice).

    Returns:
        ``len(points_ms) + 1``.
    """
    return len(points_ms) + 1


# ---------------------------------------------------------------------------
# ffmpeg wrapper (thin, isolated for monkeypatching).
# ---------------------------------------------------------------------------


def split_video(
    input_path: Path,
    output_pattern: str,
    segment_times_str: str,
    reencode: bool,
) -> int:
    """Run ffmpeg to split ``input_path`` at the given cut-points.

    Invokes::

        ffmpeg -nostdin -v error -y -i INPUT -map 0 [CODEC]
               -f segment -segment_times TIMES
               -segment_start_number 1 -reset_timestamps 1 OUTPUT_PATTERN

    where ``CODEC`` is either ``-c copy`` (stream copy) or
    ``-c:v libx264 -preset medium -crf 20 -c:a aac -b:a 128k`` (re-encode).

    Output file numbering starts at 1 (``-segment_start_number 1``) so
    parts are named ``<stem>_part1``, ``<stem>_part2``, etc.

    Args:
        input_path: Source video file.
        output_pattern: ffmpeg printf pattern for part filenames.
        segment_times_str: Comma-separated cut times in seconds (from
            ``format_segment_times``).
        reencode: When True, use libx264/aac; when False, use ``-c copy``.

    Returns:
        ffmpeg process return code (0 = success).
    """
    import subprocess

    ffmpeg_bin = resolve_ffmpeg_bin()

    if reencode:
        codec_args: list[str] = [
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
        codec_args = ["-c", "copy"]

    cmd: list[str] = [
        ffmpeg_bin,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0",
        *codec_args,
        "-f",
        "segment",
        "-segment_times",
        segment_times_str,
        "-segment_start_number",
        "1",
        "-reset_timestamps",
        "1",
        output_pattern,
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(
            "ffmpeg split failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
    return result.returncode


# ---------------------------------------------------------------------------
# CLI integration.
# ---------------------------------------------------------------------------


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the split subcommand under the videos domain parser.

    All args default to ``None`` so callers can distinguish "user did not
    supply this" from a real value; ``interactive_args`` then prompts only
    for the missing fields.
    """
    parser = subparsers.add_parser(NAME, help=DESCRIPTION)

    parser.add_argument(
        "--input",
        required=False,
        default=None,
        help="Source video file to split. Prompted if not supplied.",
    )
    parser.add_argument(
        "--output-dir",
        required=False,
        default=None,
        help=(
            "Directory for output parts. "
            "Defaults to the same directory as the input file. "
            "Created if it does not exist."
        ),
    )

    # Mutually exclusive split modes (validated in run()).
    parser.add_argument(
        "--parts",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Split into N equal-duration parts. "
            "Mutually exclusive with --segment-time."
        ),
    )
    parser.add_argument(
        "--segment-time",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Split into fixed-length chunks of SECONDS each "
            "(last chunk may be shorter). "
            "Mutually exclusive with --parts."
        ),
    )

    parser.add_argument(
        "--reencode",
        action="store_true",
        default=None,
        help=(
            "Re-encode parts with libx264/aac for frame-exact cuts. "
            "Default (absent): stream copy (-c copy), lossless and fast "
            "but cuts snap to keyframes."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=None,
        help="Skip the confirm prompt (for scripted use).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress console progress output (file log is unaffected).",
    )
    parser.set_defaults(func=run)


def _validate_args(args: argparse.Namespace) -> str | None:
    """Return None if args are valid, else a human-readable error message."""
    parts_set = args.parts is not None
    seg_time_set = args.segment_time is not None

    if parts_set and seg_time_set:
        return "--parts and --segment-time are mutually exclusive; specify exactly one"
    if not parts_set and not seg_time_set:
        return "exactly one of --parts or --segment-time is required"
    if parts_set and args.parts < MIN_PARTS:
        return f"--parts must be at least {MIN_PARTS}; got {args.parts}"
    if seg_time_set and args.segment_time <= 0:
        return f"--segment-time must be positive; got {args.segment_time}"
    return None


def interactive_args(
    prefilled: argparse.Namespace | None = None,
) -> argparse.Namespace:
    """Fill any missing split args via questionary prompts.

    A field is treated as "user did not supply" when it is ``None`` on
    ``prefilled``. Only ``--input`` triggers interactive mode; if it is
    already supplied (scripted invocation), every other unset field falls
    back to its module-level default. ``yes`` and ``quiet`` are never
    prompted for.

    If the user aborts a prompt (Ctrl-C), questionary returns ``None`` and
    that ``None`` is propagated so the caller can detect the abort.
    """
    import questionary

    base = prefilled if prefilled is not None else argparse.Namespace()

    input_value = getattr(base, "input", None)
    output_dir = getattr(base, "output_dir", None)
    parts = getattr(base, "parts", None)
    segment_time = getattr(base, "segment_time", None)
    reencode = getattr(base, "reencode", None)

    interactive_mode = input_value is None

    if input_value is None:
        input_value = questionary.path("Input video file:").ask()

    # ``--reencode`` interactive prompt: offer the two-option select that the
    # user asked for as "the last step before proceeding to the video edit."
    # Only prompt when in interactive mode AND the flag was not passed on CLI.
    if interactive_mode and reencode is None:
        method_label = questionary.select(
            "Split method:",
            choices=[_METHOD_STREAM_COPY_LABEL, _METHOD_REENCODE_LABEL],
            default=_METHOD_STREAM_COPY_LABEL,
        ).ask()
        if method_label is None:
            reencode = None  # user aborted
        else:
            reencode = method_label == _METHOD_REENCODE_LABEL

    # Apply defaults for scripted invocation path (both args supplied).
    if reencode is None and not interactive_mode:
        reencode = False

    yes = bool(getattr(base, "yes", False))
    quiet = bool(getattr(base, "quiet", False))

    return argparse.Namespace(
        input=input_value,
        output_dir=output_dir,
        parts=parts,
        segment_time=segment_time,
        reencode=reencode,
        yes=yes,
        quiet=quiet,
    )


def _build_plan(
    input_path: Path,
    output_dir: Path,
    duration_ms: int,
    args: argparse.Namespace,
) -> SplitPlan:
    """Compute cut-points and assemble a ``SplitPlan`` from validated args.

    Raises:
        ValueError: propagated from ``compute_equal_split_points`` or
            ``compute_segment_time_points`` if the derived parameters are
            logically invalid (e.g. segment >= duration).
    """
    if args.parts is not None:
        points_ms = compute_equal_split_points(duration_ms, args.parts)
        mode_label = f"equal {args.parts} parts"
    else:
        segment_ms = round(args.segment_time * SECONDS_TO_MS)
        points_ms = compute_segment_time_points(duration_ms, segment_ms)
        mode_label = f"fixed {args.segment_time:.3f}s chunks"

    return SplitPlan(
        input_path=input_path,
        output_dir=output_dir,
        points_ms=points_ms,
        reencode=bool(args.reencode),
        mode_label=mode_label,
    )


def _log_plan_preview(plan: SplitPlan, output_pattern: str) -> None:
    """Emit a structured INFO preview of what the split will do."""
    method = "re-encode (libx264/aac)" if plan.reencode else "stream copy (lossless)"
    segment_times_str = format_segment_times(plan.points_ms)
    n_parts = expected_part_count(plan.points_ms)
    logger.info(
        "Split plan:\n"
        "  input:         %s\n"
        "  mode:          %s\n"
        "  cut points:    %s\n"
        "  expected parts:%d\n"
        "  method:        %s\n"
        "  output pattern:%s",
        plan.input_path,
        plan.mode_label,
        segment_times_str or "(none — zero cut-points; would produce 1 part)",
        n_parts,
        method,
        output_pattern,
    )


def run(args: argparse.Namespace) -> int:
    """Execute the split op. Returns an exit code per HANDOFF Section 7."""
    args = interactive_args(prefilled=args)

    # Detect user abort (Ctrl-C inside a questionary prompt yields None).
    if args.input is None or args.reencode is None:
        logger.error("error: aborted by user")
        return EXIT_USER_ABORT

    # Validate mutually-exclusive split mode flags.
    error_message = _validate_args(args)
    if error_message is not None:
        logger.error("error: %s", error_message)
        return EXIT_SETUP_ERROR

    # Resolve and validate input path.
    input_path_raw = normalize_path_input(args.input)
    if input_path_raw is None:
        logger.error("error: --input path is empty or invalid")
        return EXIT_SETUP_ERROR
    input_path = input_path_raw.expanduser().resolve()
    if not input_path.is_file():
        logger.error("error: input file not found: %s", input_path)
        return EXIT_SETUP_ERROR

    # Resolve output directory (default: same directory as input file).
    if args.output_dir is not None:
        output_dir_raw = normalize_path_input(args.output_dir)
        if output_dir_raw is None:
            logger.error("error: --output-dir path is empty or invalid")
            return EXIT_SETUP_ERROR
        output_dir = output_dir_raw.expanduser().resolve()
    else:
        output_dir = input_path.parent

    output_dir.mkdir(parents=True, exist_ok=True)

    # Verify ffmpeg / ffprobe are on PATH before doing any heavy work.
    try:
        resolve_ffmpeg_bin()
        resolve_ffprobe_bin()
    except MissingDependencyError as exc:
        logger.error("error: %s", exc)
        return EXIT_SETUP_ERROR

    # Probe duration.
    try:
        duration_ms = get_duration_ms(input_path)
    except (MissingDependencyError, RuntimeError, Exception) as exc:
        logger.error("error: could not probe duration of %s: %s", input_path, exc)
        return EXIT_SETUP_ERROR

    # Compute plan.
    try:
        plan = _build_plan(input_path, output_dir, duration_ms, args)
    except ValueError as exc:
        logger.error("error: %s", exc)
        return EXIT_SETUP_ERROR

    output_pattern = build_output_pattern(input_path, output_dir)
    segment_times_str = format_segment_times(plan.points_ms)

    # Show plan preview (always; before any interactive prompt).
    _log_plan_preview(plan, output_pattern)

    # Interactive reencode prompt (non-interactive / already-set path skips
    # this; it was handled in interactive_args above).
    # Confirm prompt — skipped when --yes is set.
    if not args.yes:
        import questionary

        confirmed = questionary.confirm(
            "Proceed with the split?", default=True
        ).ask()
        if confirmed is None:
            logger.warning("aborted by user")
            return EXIT_USER_ABORT
        if not confirmed:
            logger.info("user declined; no files split")
            return EXIT_OK

    logger.info(
        "splitting %s → %d part(s) via %s ...",
        input_path.name,
        expected_part_count(plan.points_ms),
        "re-encode" if plan.reencode else "stream copy",
    )
    rc = split_video(
        input_path=input_path,
        output_pattern=output_pattern,
        segment_times_str=segment_times_str,
        reencode=plan.reencode,
    )
    if rc != 0:
        logger.error("error: ffmpeg split failed (rc=%d)", rc)
        return EXIT_ITEM_FAILED

    logger.info(
        "done: %d part(s) written to %s",
        expected_part_count(plan.points_ms),
        output_dir,
    )
    return EXIT_OK
