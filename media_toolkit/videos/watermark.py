"""Overlay an image or text watermark on one or more videos.

Supports either an image (PNG with alpha) or a free-form text string as the
watermark source. Position can be one of five static presets (corners +
center) OR a time-varying motion mode (``bounce`` Pong-style or ``drift``
slow pseudo-random wander). Single-file and batch (directory + glob) modes
share the same code path.

Pure logic helpers (position/motion expression builders, drawtext escaping,
plan construction, preview formatting) live above the I/O wrappers so they
are unit-testable without ffmpeg/ffprobe being installed. The thin
subprocess wrappers are exercised via monkeypatched ``subprocess.run``.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from media_toolkit.path_utils import normalize_path_input

logger = logging.getLogger(__name__)

NAME = "watermark"
DESCRIPTION = (
    "Overlay an image or text watermark on a video (single file or batch)."
)

# Exit codes (mirrors HANDOFF Section 7).
EXIT_OK = 0
EXIT_ITEM_FAILED = 1
EXIT_SETUP_ERROR = 2
EXIT_USER_ABORT = 130

# Position presets — produce ffmpeg overlay/drawtext (x, y) expressions.
POSITION_TOP_LEFT = "top-left"
POSITION_TOP_RIGHT = "top-right"
POSITION_BOTTOM_LEFT = "bottom-left"
POSITION_BOTTOM_RIGHT = "bottom-right"
POSITION_CENTER = "center"
POSITION_PRESETS: tuple[str, ...] = (
    POSITION_TOP_LEFT,
    POSITION_TOP_RIGHT,
    POSITION_BOTTOM_LEFT,
    POSITION_BOTTOM_RIGHT,
    POSITION_CENTER,
)

# Motion modes
MOTION_STATIC = "static"
MOTION_BOUNCE = "bounce"
MOTION_DRIFT = "drift"
MOTION_MODES: tuple[str, ...] = (MOTION_STATIC, MOTION_BOUNCE, MOTION_DRIFT)

# Default values
DEFAULT_POSITION = POSITION_BOTTOM_RIGHT
DEFAULT_MOTION = MOTION_STATIC
DEFAULT_MARGIN_PX = 20
DEFAULT_OPACITY = 0.5
DEFAULT_IMAGE_SCALE = 0.15  # fraction of main video width
DEFAULT_FONT_SIZE = 36
DEFAULT_FONT_COLOR = "white"
DEFAULT_MOTION_SPEED = 1.0
DEFAULT_BATCH_PATTERN = "*.mp4"

# Default fontfile for drawtext. Snap-confined ffmpeg cannot parse the host
# /etc/fonts/fonts.conf (its xmlns:its schema isn't recognised), so fontconfig
# lookup fails entirely. Bypass it by passing an explicit fontfile=PATH.
# DejaVuSans is preinstalled on Ubuntu/WSL at this path.
DEFAULT_FONT_FILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# CJK-capable font preinstalled via the Ubuntu ``fonts-wqy-zenhei`` package.
# Used automatically when --text contains CJK chars and the user did not
# pass --font-file (DejaVuSans renders CJK as tofu boxes).
DEFAULT_CJK_FONT_FILE = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"

# CJK Unicode ranges that require a CJK font for proper rendering.
# (CJK Unified Ideographs, CJK Extension A, Hiragana, Katakana, Hangul.)
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs (中文/日文 Han)
    (0x3400, 0x4DBF),    # CJK Extension A (rare Han)
    (0x3040, 0x309F),    # Hiragana (ひらがな)
    (0x30A0, 0x30FF),    # Katakana (カタカナ)
    (0xAC00, 0xD7AF),    # Hangul Syllables (한글)
)

# Encoder choices.
ENCODER_CPU = "cpu"
ENCODER_GPU = "gpu"
ENCODER_CHOICES: tuple[str, ...] = (ENCODER_CPU, ENCODER_GPU)
DEFAULT_ENCODER = ENCODER_CPU

# Re-encode codec settings (watermark requires re-encode; can't stream-copy).
# CPU encoder (libx264) — portable, slightly better quality at the same bitrate.
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "medium"
VIDEO_CRF = 20

# GPU encoder (NVIDIA NVENC) — ~10x faster, requires apt ffmpeg + working CUDA.
GPU_VIDEO_CODEC = "h264_nvenc"
GPU_PRESET = "fast"
GPU_CQ = 23

AUDIO_CODEC_COPY = "copy"  # audio passes through unchanged

# Bounce speeds in pixels/sec at motion-speed=1.0.
BOUNCE_SPEED_X_PX_PER_SEC = 80.0
BOUNCE_SPEED_Y_PX_PER_SEC = 60.0
# Drift uses incommensurate angular frequencies for pseudo-random look.
# Multiplied by main video width / height so the period scales with frame size.
DRIFT_FREQ_X = 0.123
DRIFT_FREQ_Y = 0.078

PREVIEW_LIMIT = 50
VIDEO_EXTENSIONS: tuple[str, ...] = (
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".avi",
    ".m4v",
)

# Plan-entry action labels.
ACTION_PROCESS = "process"
ACTION_SKIP_CONFLICT = "skip-conflict"
ACTION_OVERWRITE = "overwrite"
SKIP_ACTIONS: tuple[str, ...] = (ACTION_SKIP_CONFLICT,)

# Bounds for visual params (validated in run()).
OPACITY_MIN = 0.0
OPACITY_MAX = 1.0
SCALE_MIN_EXCLUSIVE = 0.0  # scale must be > 0
SCALE_MAX = 1.0

# ffmpeg / ffprobe binary discovery.
#
# Duplicated from videos/concat.py — refactor to shared module
# (``media_toolkit/_ffmpeg.py``) when a third op needs it. Two ops is not yet
# enough copy-paste to justify the indirection.
FFMPEG_CANDIDATES: tuple[str, ...] = ("ffmpeg",)
FFPROBE_CANDIDATES: tuple[str, ...] = ("ffprobe", "ffmpeg.ffprobe")
_RESOLVED_FFMPEG_BIN: str | None = None
_RESOLVED_FFPROBE_BIN: str | None = None


class WatermarkError(Exception):
    """Base error for the watermark op."""


class WatermarkSetupError(WatermarkError):
    """Raised for invalid args or missing dependencies."""


class MissingDependencyError(WatermarkSetupError):
    """Raised when ffmpeg / ffprobe are not on PATH."""


@dataclass(frozen=True)
class WatermarkPlanEntry:
    """One planned watermark operation."""

    source: Path
    destination: Path
    action: str  # ACTION_PROCESS / ACTION_SKIP_CONFLICT / ACTION_OVERWRITE


# ---------------------------------------------------------------------------
# Pure logic (no ffmpeg / ffprobe required; tested directly).
# ---------------------------------------------------------------------------


def position_to_xy_expressions(
    position: str,
    margin: int,
    *,
    is_text: bool,
) -> tuple[str, str]:
    """Map a position preset to ffmpeg (x_expr, y_expr) string expressions.

    The expressions reference ffmpeg's runtime variables: ``W``/``H`` for the
    main video, and ``w``/``h`` (overlay) or ``tw``/``th`` (drawtext) for the
    watermark. ``is_text=True`` selects the drawtext variable names.
    """
    if position not in POSITION_PRESETS:
        raise ValueError(
            f"unknown position preset: {position!r}; "
            f"expected one of {POSITION_PRESETS}"
        )
    w_var = "tw" if is_text else "w"
    h_var = "th" if is_text else "h"
    if position == POSITION_TOP_LEFT:
        return (str(margin), str(margin))
    if position == POSITION_TOP_RIGHT:
        return (f"W-{w_var}-{margin}", str(margin))
    if position == POSITION_BOTTOM_LEFT:
        return (str(margin), f"H-{h_var}-{margin}")
    if position == POSITION_BOTTOM_RIGHT:
        return (f"W-{w_var}-{margin}", f"H-{h_var}-{margin}")
    # POSITION_CENTER
    return (f"(W-{w_var})/2", f"(H-{h_var})/2")


def motion_to_xy_expressions(
    motion: str,
    speed: float,
    *,
    is_text: bool,
) -> tuple[str, str]:
    """Build time-varying (x_expr, y_expr) for non-static motion modes.

    ``speed`` linearly multiplies the base motion rate (1.0 = default).
    Raises ``ValueError`` if ``motion`` is ``MOTION_STATIC`` (callers must
    use ``position_to_xy_expressions`` for static positioning) or unknown.
    """
    if motion == MOTION_STATIC:
        raise ValueError(
            "motion_to_xy_expressions cannot handle MOTION_STATIC; "
            "use position_to_xy_expressions instead"
        )
    if motion not in MOTION_MODES:
        raise ValueError(
            f"unknown motion mode: {motion!r}; expected one of {MOTION_MODES}"
        )
    w_var = "tw" if is_text else "w"
    h_var = "th" if is_text else "h"

    if motion == MOTION_BOUNCE:
        # Pong-style triangle wave bouncing between [0, W-w] and [0, H-h].
        # mod(SX*t, 2*(W-w)) - (W-w)  spans [-(W-w), +(W-w)]; abs() folds to
        # [0, W-w]. Same pattern for Y.
        sx = BOUNCE_SPEED_X_PX_PER_SEC * speed
        sy = BOUNCE_SPEED_Y_PX_PER_SEC * speed
        x_expr = (
            f"abs(mod({sx}*t,2*(W-{w_var}))-(W-{w_var}))"
        )
        y_expr = (
            f"abs(mod({sy}*t,2*(H-{h_var}))-(H-{h_var}))"
        )
        return (x_expr, y_expr)

    # MOTION_DRIFT: incommensurate freqs scaled by main dims for slow wander.
    # mod() keeps the watermark inside the visible canvas at all times.
    fx = DRIFT_FREQ_X * speed
    fy = DRIFT_FREQ_Y * speed
    x_expr = f"mod({fx}*W*t,W-{w_var})"
    y_expr = f"mod({fy}*H*t,H-{h_var})"
    return (x_expr, y_expr)


def _resolve_xy_expressions(
    position: str,
    margin: int,
    motion: str,
    motion_speed: float,
    *,
    is_text: bool,
) -> tuple[str, str]:
    """Pick the right (x, y) expressions based on motion mode."""
    if motion == MOTION_STATIC:
        return position_to_xy_expressions(position, margin, is_text=is_text)
    return motion_to_xy_expressions(motion, motion_speed, is_text=is_text)


def _escape_ffmpeg_expr(expr: str) -> str:
    """Escape an ffmpeg filter expression for embedding inside an option value.

    ffmpeg uses ',' to separate filters in a chain and ':' to separate options.
    Both must be backslash-escaped when they appear inside an expression embedded
    in an option value (e.g. drawtext's x=... or overlay's overlay=...).

    Without this, motion expressions like ``abs(mod(80*t,2*(W-tw))-(W-tw))``
    get split at the literal ``,`` inside ``mod(...)`` and ffmpeg reports
    "No such filter: '2*(W-tw))-(W-tw)):y'".

    Backslashes themselves must be escaped first to avoid double-processing.
    See: https://ffmpeg.org/ffmpeg-filters.html#Notes-on-filtergraph-escaping
    """
    return expr.replace("\\", "\\\\").replace(",", "\\,").replace(":", "\\:")


def build_overlay_filter(
    image_path: Path,
    opacity: float,
    scale_pixels_w: int,
    position: str,
    margin: int,
    motion: str,
    motion_speed: float,
) -> str:
    """Build the -filter_complex argument for an image watermark.

    ``scale_pixels_w`` is the absolute target width in pixels for the
    watermark image (height auto-computed via ``-2`` to preserve aspect
    ratio while staying divisible by 2 for libx264). The caller is
    expected to derive this from ``main_video_width * scale_fraction``
    after probing the main video — this avoids the fiddly ``scale2ref``
    filter chain.

    ``image_path`` itself is NOT embedded in the filter string; it gets
    passed as a second ``-i`` input to ffmpeg by the caller.

    Resulting filter shape:
        [1:v]format=rgba,colorchannelmixer=aa=OP,scale=W_PX:-2[wm];
        [0:v][wm]overlay=X_EXPR:Y_EXPR
    """
    x_expr, y_expr = _resolve_xy_expressions(
        position, margin, motion, motion_speed, is_text=False
    )
    # Escape ',' and ':' inside motion expressions so ffmpeg doesn't treat
    # them as filter-chain / option separators (see _escape_ffmpeg_expr).
    x_safe = _escape_ffmpeg_expr(x_expr)
    y_safe = _escape_ffmpeg_expr(y_expr)
    # Ensure even pixel width (libx264 requires even dims). trunc(/2)*2 is a
    # safety net even though the caller usually passes an int already.
    even_width = max(2, (int(scale_pixels_w) // 2) * 2)
    return (
        f"[1:v]format=rgba,colorchannelmixer=aa={opacity},"
        f"scale={even_width}:-2[wm];"
        f"[0:v][wm]overlay={x_safe}:{y_safe}"
    )


def has_cjk_chars(text: str) -> bool:
    """True if ``text`` contains any character in a CJK Unicode range that
    needs a CJK-capable font for correct rendering."""
    for ch in text:
        cp = ord(ch)
        for start, end in _CJK_RANGES:
            if start <= cp <= end:
                return True
    return False


def select_font_file(text: str, user_font_file: Path | None) -> Path:
    """Pick the font file to pass to ffmpeg drawtext.

    Priority:
      1. If ``user_font_file`` is set, always honor it (user knows what they want).
      2. If ``text`` has CJK chars and DEFAULT_CJK_FONT_FILE exists, use it.
      3. If ``text`` has CJK chars but no CJK font is installed, log a warning
         and fall back to DEFAULT_FONT_FILE (text will render as boxes).
      4. Otherwise (plain ASCII), use DEFAULT_FONT_FILE.
    """
    if user_font_file is not None:
        return user_font_file
    if has_cjk_chars(text):
        cjk_path = Path(DEFAULT_CJK_FONT_FILE)
        if cjk_path.exists():
            logger.info(
                "CJK characters detected in --text; using CJK font: %s",
                cjk_path,
            )
            return cjk_path
        logger.warning(
            "text contains CJK characters but no CJK font installed at %s; "
            "watermark will likely render as boxes. Install fonts-wqy-zenhei "
            "or pass --font-file pointing to a CJK-capable font.",
            DEFAULT_CJK_FONT_FILE,
        )
    return Path(DEFAULT_FONT_FILE)


def _build_video_codec_args(encoder: str) -> list[str]:
    """Return the ffmpeg ``-c:v`` / preset / quality args for the chosen encoder."""
    if encoder == ENCODER_GPU:
        return [
            "-c:v",
            GPU_VIDEO_CODEC,
            "-preset",
            GPU_PRESET,
            "-cq",
            str(GPU_CQ),
        ]
    return [
        "-c:v",
        VIDEO_CODEC,
        "-preset",
        VIDEO_PRESET,
        "-crf",
        str(VIDEO_CRF),
    ]


def _escape_drawtext_text(text: str) -> str:
    """Escape a string for ffmpeg's drawtext ``text=`` parameter.

    drawtext interprets these characters specially inside a single-quoted
    text value: backslash, single quote, colon, percent. Backslash MUST be
    escaped first so we don't double-escape sequences we just inserted.
    Single quotes terminate the value, so we close-quote, emit a literal
    escaped quote, then re-open: ``'\''`` (4 chars).
    """
    # Order matters: backslash first.
    escaped = text.replace("\\", "\\\\")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("%", "\\%")
    escaped = escaped.replace("'", "'\\''")
    return escaped


def build_drawtext_filter(
    text: str,
    font_size: int,
    font_color: str,
    opacity: float,
    position: str,
    margin: int,
    motion: str,
    motion_speed: float,
    font_file: str | None = None,
) -> str:
    """Build the -vf argument for a text watermark via ffmpeg's drawtext.

    Result shape (single line):
        drawtext=fontfile=PATH:text='ESCAPED':fontsize=SZ:
                 fontcolor=COLOR@OP:x=X:y=Y

    ``font_file`` defaults to ``DEFAULT_FONT_FILE`` (DejaVuSans on Ubuntu/WSL)
    so we bypass fontconfig entirely — snap-confined ffmpeg can't parse the
    host fonts.conf and would otherwise fail with "Cannot load config file
    from /etc/fonts/fonts.conf". Pass an explicit path here (e.g. a CJK font
    for non-ASCII text).

    If ``font_file`` is None and ``text`` contains CJK characters, the auto-
    selector (``select_font_file``) substitutes ``DEFAULT_CJK_FONT_FILE`` so
    Chinese / Japanese / Korean glyphs render correctly instead of boxes.
    """
    x_expr, y_expr = _resolve_xy_expressions(
        position, margin, motion, motion_speed, is_text=True
    )
    # Escape ',' and ':' inside motion expressions (see _escape_ffmpeg_expr).
    x_safe = _escape_ffmpeg_expr(x_expr)
    y_safe = _escape_ffmpeg_expr(y_expr)
    escaped_text = _escape_drawtext_text(text)
    user_font_path: Path | None = (
        Path(font_file) if font_file is not None else None
    )
    font_path = select_font_file(text, user_font_path)
    # The fontfile path is also an option value, so escape ':' and ',' in it.
    # On WSL the default path has no special chars, but a Windows-style path
    # like "D:/fonts/x.ttf" or any path with a comma would break things.
    safe_font = _escape_ffmpeg_expr(str(font_path))
    return (
        f"drawtext=fontfile={safe_font}:text='{escaped_text}':"
        f"fontsize={font_size}:"
        f"fontcolor={font_color}@{opacity}:x={x_safe}:y={y_safe}"
    )


def _has_video_extension(path: Path) -> bool:
    """True if ``path``'s suffix is in VIDEO_EXTENSIONS (case-insensitive)."""
    return path.suffix.lower() in VIDEO_EXTENSIONS


def discover_videos(input_path: Path, pattern: str) -> list[Path]:
    """Return the list of video files to process.

    If ``input_path`` is a single file, returns ``[input_path]`` after
    validating its extension is in ``VIDEO_EXTENSIONS``. If a directory,
    runs ``input_path.glob(pattern)`` and returns the sorted file matches.
    Raises ``FileNotFoundError`` if the path does not exist.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"input path does not exist: {input_path}")
    if input_path.is_file():
        if not _has_video_extension(input_path):
            raise ValueError(
                f"input file does not have a recognised video extension "
                f"({', '.join(VIDEO_EXTENSIONS)}): {input_path}"
            )
        return [input_path]
    return sorted(p for p in input_path.glob(pattern) if p.is_file())


def build_watermark_plan(
    videos: list[Path],
    input_path: Path,
    output_path: Path,
    overwrite: bool,
) -> list[WatermarkPlanEntry]:
    """Build a per-video plan mapping each source to its destination.

    Mapping rules:
        * If ``input_path`` is a file, ``output_path`` is treated as a file
          path (single-file mode).
        * If ``input_path`` is a directory, each video maps to
          ``output_path / video.name`` — no subdir mirror, intentional
          (callers using recursive globs that produce duplicates would
          otherwise collide; opting out is simpler than detecting it).

    Action assignment:
        * ACTION_OVERWRITE if dest exists and ``overwrite`` is True
        * ACTION_SKIP_CONFLICT if dest exists and ``overwrite`` is False
        * ACTION_PROCESS otherwise
    """
    is_file_mode = input_path.is_file()
    plan: list[WatermarkPlanEntry] = []
    for video in videos:
        if is_file_mode:
            destination = output_path
        else:
            destination = output_path / video.name

        if destination.exists():
            action = ACTION_OVERWRITE if overwrite else ACTION_SKIP_CONFLICT
        else:
            action = ACTION_PROCESS
        plan.append(
            WatermarkPlanEntry(
                source=video, destination=destination, action=action
            )
        )
    return plan


def _format_plan_line(entry: WatermarkPlanEntry) -> str:
    """Render one plan entry as a human-readable preview line."""
    if entry.action == ACTION_SKIP_CONFLICT:
        return (
            f"skip       {entry.source.name} "
            f"(dest exists; pass --overwrite to replace)"
        )
    if entry.action == ACTION_OVERWRITE:
        return (
            f"overwrite  {entry.source.name} -> {entry.destination} "
            f"(replacing existing)"
        )
    return f"process    {entry.source.name} -> {entry.destination}"


def format_preview(
    plan: list[WatermarkPlanEntry], limit: int = PREVIEW_LIMIT
) -> str:
    """Render the watermark plan as a preview block plus summary tail.

    Truncates the visible body at ``limit`` lines if the plan is larger,
    appending ``...and N more entries``. The summary tail lists only
    non-zero categories so the all-process common case stays short.
    """
    counts: dict[str, int] = {
        ACTION_PROCESS: 0,
        ACTION_OVERWRITE: 0,
        ACTION_SKIP_CONFLICT: 0,
    }
    for entry in plan:
        counts[entry.action] = counts.get(entry.action, 0) + 1

    lines: list[str] = []
    visible = plan if len(plan) <= limit else plan[:limit]
    for entry in visible:
        lines.append(_format_plan_line(entry))

    if len(plan) > limit:
        remaining = len(plan) - limit
        lines.append(f"...and {remaining} more entries")

    summary_parts: list[str] = [f"{counts[ACTION_PROCESS]} to process"]
    if counts[ACTION_OVERWRITE]:
        summary_parts.append(f"{counts[ACTION_OVERWRITE]} to overwrite")
    if counts[ACTION_SKIP_CONFLICT]:
        summary_parts.append(
            f"{counts[ACTION_SKIP_CONFLICT]} to skip (conflict)"
        )
    lines.append("Total: " + ", ".join(summary_parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe wrappers (isolated for monkeypatch testing).
# ---------------------------------------------------------------------------


def _resolve_binary(candidates: tuple[str, ...]) -> str:
    """Return the first executable in ``candidates`` resolvable on PATH."""
    for name in candidates:
        if shutil.which(name) is not None:
            return name
    raise MissingDependencyError(
        f"none of these executables found on PATH: {', '.join(candidates)}"
    )


def _ensure_dependencies() -> None:
    """Resolve ffmpeg / ffprobe binary names and cache them at module level."""
    global _RESOLVED_FFMPEG_BIN, _RESOLVED_FFPROBE_BIN
    _RESOLVED_FFMPEG_BIN = _resolve_binary(FFMPEG_CANDIDATES)
    _RESOLVED_FFPROBE_BIN = _resolve_binary(FFPROBE_CANDIDATES)


def _get_ffmpeg_bin() -> str:
    """Return the resolved ffmpeg binary name (auto-resolves if needed)."""
    if _RESOLVED_FFMPEG_BIN is None:
        _ensure_dependencies()
    assert _RESOLVED_FFMPEG_BIN is not None
    return _RESOLVED_FFMPEG_BIN


def _get_ffprobe_bin() -> str:
    """Return the resolved ffprobe binary name (auto-resolves if needed)."""
    if _RESOLVED_FFPROBE_BIN is None:
        _ensure_dependencies()
    assert _RESOLVED_FFPROBE_BIN is not None
    return _RESOLVED_FFPROBE_BIN


def get_video_dimensions(video_path: Path) -> tuple[int, int]:
    """Return ``(width, height)`` of the first video stream via ffprobe."""
    result = subprocess.run(
        [
            _get_ffprobe_bin(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(video_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WatermarkError(
            f"ffprobe failed for {video_path}: {result.stderr.strip()}"
        )
    raw = result.stdout.strip()
    if "x" not in raw:
        raise WatermarkError(
            f"ffprobe returned unexpected dimension output {raw!r} "
            f"for {video_path}"
        )
    width_str, height_str = raw.split("x", 1)
    return (int(width_str), int(height_str))


def apply_watermark_to_video(
    source: Path,
    destination: Path,
    filter_arg: str,
    is_image_watermark: bool,
    image_path: Path | None,
    encoder: str = DEFAULT_ENCODER,
) -> int:
    """Run ffmpeg to apply ``filter_arg`` to ``source``, writing to ``destination``.

    For image watermarks, ``image_path`` is added as a second ``-i`` input
    and ``filter_arg`` goes through ``-filter_complex``. For text
    watermarks, ``filter_arg`` goes through ``-vf`` and there is no second
    input. Audio always passes through with ``-c:a copy``; ``-nostdin``
    keeps ffmpeg from consuming the parent's stdin in heredoc / piped
    contexts. The video codec is selected by ``encoder``: ``"cpu"`` uses
    libx264, ``"gpu"`` uses h264_nvenc.
    """
    if is_image_watermark and image_path is None:
        raise ValueError(
            "image_path is required when is_image_watermark is True"
        )

    cmd: list[str] = [
        _get_ffmpeg_bin(),
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
    ]
    if is_image_watermark:
        # image_path is non-None per the guard above; assert for type narrowing.
        assert image_path is not None
        cmd.extend(["-i", str(image_path), "-filter_complex", filter_arg])
    else:
        cmd.extend(["-vf", filter_arg])
    cmd.extend(_build_video_codec_args(encoder))
    cmd.extend(
        [
            "-c:a",
            AUDIO_CODEC_COPY,
            str(destination),
        ]
    )

    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        # Log at ERROR with full stderr so failures are visible in
        # CLI logs (was WARNING; bumped so a swallowed stderr never
        # leaves the user staring at "rc=1" with no diagnostic).
        logger.error(
            "ffmpeg watermark failed for %s: rc=%d; cmd=%s; stderr:\n%s",
            source,
            result.returncode,
            " ".join(cmd),
            result.stderr.strip(),
        )
    return result.returncode


def execute_plan(
    plan: list[WatermarkPlanEntry],
    filter_arg: str,
    is_image: bool,
    image: Path | None,
    log: logging.Logger,
    encoder: str = DEFAULT_ENCODER,
) -> tuple[int, int, int]:
    """Apply the plan via per-entry ffmpeg invocations.

    Returns ``(processed, skipped, failed)``. Each entry is wrapped in its
    own try/except so one ffmpeg failure does not abort a batch.
    """
    processed = 0
    skipped = 0
    failed = 0
    for entry in plan:
        if entry.action in SKIP_ACTIONS:
            log.info(
                "skip (%s): %s -> %s",
                entry.action,
                entry.source,
                entry.destination,
            )
            skipped += 1
            continue
        try:
            entry.destination.parent.mkdir(parents=True, exist_ok=True)
            rc = apply_watermark_to_video(
                source=entry.source,
                destination=entry.destination,
                filter_arg=filter_arg,
                is_image_watermark=is_image,
                image_path=image,
                encoder=encoder,
            )
            if rc == 0:
                log.info("processed: %s -> %s", entry.source, entry.destination)
                processed += 1
            else:
                log.error(
                    "failed (rc=%d): %s -> %s",
                    rc,
                    entry.source,
                    entry.destination,
                )
                failed += 1
        except (OSError, WatermarkError) as exc:
            log.error(
                "failed to watermark %s -> %s: %s",
                entry.source,
                entry.destination,
                exc,
            )
            failed += 1
    return processed, skipped, failed


# ---------------------------------------------------------------------------
# CLI integration.
# ---------------------------------------------------------------------------


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the watermark subcommand under the videos domain parser."""
    parser = subparsers.add_parser(NAME, help=DESCRIPTION)

    # Input / output.
    parser.add_argument(
        "--input",
        required=False,
        default=None,
        help="Single video file OR directory of videos to process.",
    )
    parser.add_argument(
        "--output",
        required=False,
        default=None,
        help=(
            "Output file (when --input is a file) or output directory "
            "(when --input is a directory)."
        ),
    )

    # Watermark content (mutually exclusive — enforced in run()).
    parser.add_argument(
        "--image",
        required=False,
        default=None,
        help="PNG image to use as watermark (use with --opacity, --scale).",
    )
    parser.add_argument(
        "--text",
        required=False,
        default=None,
        help=(
            "Text string to use as watermark "
            "(use with --font-size, --font-color, --opacity)."
        ),
    )

    # Positioning.
    parser.add_argument(
        "--position",
        choices=POSITION_PRESETS,
        default=None,
        help=(
            f"Static position preset (default: {DEFAULT_POSITION}). "
            "Ignored when --motion is not 'static'."
        ),
    )
    parser.add_argument(
        "--margin",
        type=int,
        default=None,
        help=(
            f"Pixels from edge for static positions "
            f"(default: {DEFAULT_MARGIN_PX})."
        ),
    )

    # Motion.
    parser.add_argument(
        "--motion",
        choices=MOTION_MODES,
        default=None,
        help=(
            f"Watermark motion mode (default: {DEFAULT_MOTION}). "
            "'bounce' = diagonal Pong; 'drift' = slow pseudo-random wander."
        ),
    )
    parser.add_argument(
        "--motion-speed",
        type=float,
        default=None,
        help=(
            f"Motion speed multiplier (default: {DEFAULT_MOTION_SPEED}). "
            "Higher = faster."
        ),
    )

    # Visual params.
    parser.add_argument(
        "--opacity",
        type=float,
        default=None,
        help=f"0.0-1.0 (default: {DEFAULT_OPACITY}).",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=None,
        help=(
            f"Image scale relative to video width "
            f"(default: {DEFAULT_IMAGE_SCALE}). Image mode only."
        ),
    )
    parser.add_argument(
        "--font-size",
        type=int,
        default=None,
        help=f"Text mode only (default: {DEFAULT_FONT_SIZE}).",
    )
    parser.add_argument(
        "--font-color",
        default=None,
        help=(
            f"Text mode only (default: {DEFAULT_FONT_COLOR}). "
            "ffmpeg colour name or '#RRGGBB'."
        ),
    )
    parser.add_argument(
        "--font-file",
        required=False,
        default=None,
        help=(
            f"Path to TTF/OTF font file (default: {DEFAULT_FONT_FILE}). "
            f"Use a CJK font for Chinese/Japanese/Korean watermark text. "
            f"Bypasses fontconfig (required on snap-confined ffmpeg)."
        ),
    )

    # Batch.
    parser.add_argument(
        "--pattern",
        default=None,
        help=(
            f"Glob pattern when --input is a directory "
            f"(default: '{DEFAULT_BATCH_PATTERN}'). Use '**/*.mp4' to recurse."
        ),
    )

    # Encoder.
    parser.add_argument(
        "--encoder",
        choices=ENCODER_CHOICES,
        default=None,
        help=(
            f"Video encoder (default: {DEFAULT_ENCODER}). "
            f"'gpu' uses NVIDIA NVENC (~10x faster, requires apt ffmpeg + "
            f"CUDA driver). 'cpu' uses libx264 (more portable, slightly "
            f"better quality)."
        ),
    )

    # Common.
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=None,
        help="Overwrite existing output files (default: skip conflicts).",
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
        help="Suppress per-file progress output (file log unaffected).",
    )
    parser.set_defaults(func=run)


# Interactive-prompt label constants (no magic strings).
_TYPE_IMAGE_LABEL = "Image"
_TYPE_TEXT_LABEL = "Text"
_MOTION_STATIC_LABEL = "static (fixed position)"
_MOTION_BOUNCE_LABEL = "bounce (diagonal)"
_MOTION_DRIFT_LABEL = "drift (slow wander)"
_MOTION_LABEL_TO_KEY: dict[str, str] = {
    _MOTION_STATIC_LABEL: MOTION_STATIC,
    _MOTION_BOUNCE_LABEL: MOTION_BOUNCE,
    _MOTION_DRIFT_LABEL: MOTION_DRIFT,
}
_ENCODER_CPU_LABEL = "cpu (libx264, portable)"
_ENCODER_GPU_LABEL = "gpu (h264_nvenc, ~10x faster, requires NVIDIA + apt ffmpeg)"
_ENCODER_LABEL_TO_KEY: dict[str, str] = {
    _ENCODER_CPU_LABEL: ENCODER_CPU,
    _ENCODER_GPU_LABEL: ENCODER_GPU,
}


def interactive_args(
    prefilled: argparse.Namespace | None = None,
) -> argparse.Namespace:
    """Fill any missing watermark args via questionary prompts.

    A field is treated as "user did not supply" when it is ``None`` on
    ``prefilled``. Only --input and --output trigger interactive mode; if
    both are supplied (scripted invocation), every other unset field falls
    back to its module-level default. ``yes`` and ``quiet`` are never
    prompted for.
    """
    import questionary

    base = prefilled if prefilled is not None else argparse.Namespace()

    input_value = getattr(base, "input", None)
    output_value = getattr(base, "output", None)
    image_value = getattr(base, "image", None)
    text_value = getattr(base, "text", None)
    position = getattr(base, "position", None)
    margin = getattr(base, "margin", None)
    motion = getattr(base, "motion", None)
    motion_speed = getattr(base, "motion_speed", None)
    opacity = getattr(base, "opacity", None)
    scale = getattr(base, "scale", None)
    font_size = getattr(base, "font_size", None)
    font_color = getattr(base, "font_color", None)
    font_file = getattr(base, "font_file", None)
    pattern = getattr(base, "pattern", None)
    overwrite = getattr(base, "overwrite", None)
    encoder = getattr(base, "encoder", None)

    interactive_mode = input_value is None or output_value is None

    if input_value is None:
        input_value = questionary.path("Input video or directory:").ask()

    if output_value is None:
        output_value = questionary.path("Output file or directory:").ask()

    # Watermark type only matters when neither --image nor --text was given.
    if interactive_mode and image_value is None and text_value is None:
        wm_type = questionary.select(
            "Watermark type:", choices=[_TYPE_IMAGE_LABEL, _TYPE_TEXT_LABEL]
        ).ask()
        if wm_type == _TYPE_IMAGE_LABEL:
            image_value = questionary.path("Watermark image (PNG):").ask()
        elif wm_type == _TYPE_TEXT_LABEL:
            text_value = questionary.text("Watermark text:").ask()

    if interactive_mode and motion is None:
        motion_label = questionary.select(
            "Motion:",
            choices=[
                _MOTION_STATIC_LABEL,
                _MOTION_BOUNCE_LABEL,
                _MOTION_DRIFT_LABEL,
            ],
            default=_MOTION_STATIC_LABEL,
        ).ask()
        motion = (
            _MOTION_LABEL_TO_KEY.get(motion_label) if motion_label else None
        )

    # Position only meaningful for static mode.
    effective_motion = motion if motion is not None else DEFAULT_MOTION
    if interactive_mode and position is None and effective_motion == MOTION_STATIC:
        position = questionary.select(
            "Position:", choices=list(POSITION_PRESETS), default=DEFAULT_POSITION
        ).ask()

    if interactive_mode and opacity is None:
        opacity_str = questionary.text(
            "Opacity (0.0-1.0):", default=str(DEFAULT_OPACITY)
        ).ask()
        if opacity_str is not None:
            try:
                opacity = float(opacity_str)
            except ValueError:
                opacity = None

    if interactive_mode and image_value is not None and scale is None:
        scale_str = questionary.text(
            "Image scale (fraction of video width):",
            default=str(DEFAULT_IMAGE_SCALE),
        ).ask()
        if scale_str is not None:
            try:
                scale = float(scale_str)
            except ValueError:
                scale = None

    if interactive_mode and text_value is not None and font_size is None:
        font_size_str = questionary.text(
            "Font size (px):", default=str(DEFAULT_FONT_SIZE)
        ).ask()
        if font_size_str is not None:
            try:
                font_size = int(font_size_str)
            except ValueError:
                font_size = None

    if interactive_mode and encoder is None:
        encoder_label = questionary.select(
            "Encoder:",
            choices=[_ENCODER_CPU_LABEL, _ENCODER_GPU_LABEL],
            default=_ENCODER_CPU_LABEL,
        ).ask()
        encoder = (
            _ENCODER_LABEL_TO_KEY.get(encoder_label)
            if encoder_label
            else None
        )

    if interactive_mode and overwrite is None:
        overwrite = questionary.confirm(
            "Overwrite existing output files?", default=False
        ).ask()

    yes = bool(getattr(base, "yes", False))
    quiet = bool(getattr(base, "quiet", False))

    # Apply defaults for any field still unset (scripted invocation path or
    # user accepted defaults). We deliberately leave None alone where it
    # signals a real abort (Ctrl-C in questionary).
    if margin is None:
        margin = DEFAULT_MARGIN_PX
    if motion is None:
        motion = DEFAULT_MOTION
    if motion_speed is None:
        motion_speed = DEFAULT_MOTION_SPEED
    if opacity is None and not interactive_mode:
        opacity = DEFAULT_OPACITY
    if scale is None and image_value is not None and not interactive_mode:
        scale = DEFAULT_IMAGE_SCALE
    if font_size is None and text_value is not None and not interactive_mode:
        font_size = DEFAULT_FONT_SIZE
    if font_color is None:
        font_color = DEFAULT_FONT_COLOR
    if pattern is None:
        pattern = DEFAULT_BATCH_PATTERN
    if position is None and motion == MOTION_STATIC:
        position = DEFAULT_POSITION
    if overwrite is None and not interactive_mode:
        overwrite = False
    if encoder is None:
        encoder = DEFAULT_ENCODER

    return argparse.Namespace(
        input=input_value,
        output=output_value,
        image=image_value,
        text=text_value,
        position=position,
        margin=margin,
        motion=motion,
        motion_speed=motion_speed,
        opacity=opacity,
        scale=scale,
        font_size=font_size,
        font_color=font_color,
        font_file=font_file,
        pattern=pattern,
        overwrite=overwrite,
        encoder=encoder,
        yes=yes,
        quiet=quiet,
    )


def _validate_args(args: argparse.Namespace) -> str | None:
    """Return None if args are valid, else a human-readable error message."""
    image_set = args.image is not None
    text_set = args.text is not None
    if not image_set and not text_set:
        return "exactly one of --image or --text must be provided"
    if image_set and text_set:
        return "--image and --text are mutually exclusive"

    if args.opacity is None:
        return "--opacity is required (0.0-1.0)"
    if not (OPACITY_MIN <= args.opacity <= OPACITY_MAX):
        return (
            f"--opacity must be in [{OPACITY_MIN}, {OPACITY_MAX}]; "
            f"got {args.opacity}"
        )
    if image_set:
        if args.scale is None:
            return "--scale is required for image watermarks"
        if not (SCALE_MIN_EXCLUSIVE < args.scale <= SCALE_MAX):
            return (
                f"--scale must be in ({SCALE_MIN_EXCLUSIVE}, {SCALE_MAX}]; "
                f"got {args.scale}"
            )
    if text_set:
        if args.font_size is None or args.font_size <= 0:
            return f"--font-size must be a positive integer; got {args.font_size}"
    if args.motion not in MOTION_MODES:
        return f"--motion must be one of {MOTION_MODES}; got {args.motion!r}"
    if args.motion == MOTION_STATIC and args.position not in POSITION_PRESETS:
        return (
            f"--position must be one of {POSITION_PRESETS} for static motion; "
            f"got {args.position!r}"
        )
    if args.encoder not in ENCODER_CHOICES:
        return (
            f"--encoder must be one of {ENCODER_CHOICES}; "
            f"got {args.encoder!r}"
        )
    return None


def run(args: argparse.Namespace) -> int:
    """Execute the watermark op. Returns an exit code per HANDOFF Section 7."""
    args = interactive_args(prefilled=args)

    # Detect user abort (Ctrl-C inside a questionary prompt yields None).
    if args.input is None or args.output is None:
        logger.error("error: aborted by user")
        return EXIT_USER_ABORT

    error_message = _validate_args(args)
    if error_message is not None:
        logger.error("error: %s", error_message)
        return EXIT_SETUP_ERROR

    input_path = normalize_path_input(args.input).expanduser().resolve()
    output_path = normalize_path_input(args.output).expanduser().resolve()
    image_path: Path | None = None
    if args.image is not None:
        image_path = normalize_path_input(args.image).expanduser().resolve()
        if not image_path.is_file():
            logger.error("error: watermark image not found: %s", image_path)
            return EXIT_SETUP_ERROR

    try:
        videos = discover_videos(input_path, args.pattern)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("error: %s", exc)
        return EXIT_SETUP_ERROR

    if not videos:
        logger.warning(
            "no videos matched pattern %r in %s; nothing to do",
            args.pattern,
            input_path,
        )
        return EXIT_OK

    # Decide once whether output is treated as a directory (batch mode).
    is_dir_mode = input_path.is_dir()
    if is_dir_mode:
        output_path.mkdir(parents=True, exist_ok=True)

    plan = build_watermark_plan(
        videos=videos,
        input_path=input_path,
        output_path=output_path,
        overwrite=bool(args.overwrite),
    )
    logger.info("Watermark preview:\n%s", format_preview(plan))

    if not args.yes:
        import questionary

        confirmed = questionary.confirm(
            f"Proceed with {len(plan)} entries?", default=False
        ).ask()
        if confirmed is None:
            logger.warning("aborted by user")
            return EXIT_USER_ABORT
        if not confirmed:
            logger.info("user declined; no files watermarked")
            return EXIT_OK

    try:
        _ensure_dependencies()
    except MissingDependencyError as exc:
        logger.error("error: %s", exc)
        return EXIT_SETUP_ERROR

    # Build the filter once. Image mode needs the absolute pixel width of
    # the watermark, which depends on the main video width — probe the
    # first source to derive it. Assumes the batch is homogeneous in width
    # (typical for our use case where one input dir == one chapter == one
    # capture pipeline). If videos differ in width, the watermark will
    # still render but at a slightly different fraction-of-width per item.
    is_image = image_path is not None
    if is_image:
        first_video = videos[0]
        try:
            main_w, _main_h = get_video_dimensions(first_video)
        except WatermarkError as exc:
            logger.error("error: %s", exc)
            return EXIT_SETUP_ERROR
        scale_pixels_w = max(2, int(main_w * args.scale))
        # image_path is non-None inside this branch (is_image is True iff
        # we set image_path above). assert for the type checker.
        assert image_path is not None
        filter_arg = build_overlay_filter(
            image_path=image_path,
            opacity=args.opacity,
            scale_pixels_w=scale_pixels_w,
            position=args.position or DEFAULT_POSITION,
            margin=args.margin,
            motion=args.motion,
            motion_speed=args.motion_speed,
        )
    else:
        # Normalize the font path if supplied on the CLI (allows Win-style
        # input like "D:\fonts\x.ttf"); fall through to None so build_drawtext_filter
        # uses DEFAULT_FONT_FILE.
        font_file_arg: str | None = None
        if args.font_file is not None:
            font_file_arg = str(
                normalize_path_input(args.font_file).expanduser()
            )
        filter_arg = build_drawtext_filter(
            text=args.text,
            font_size=args.font_size,
            font_color=args.font_color,
            opacity=args.opacity,
            position=args.position or DEFAULT_POSITION,
            margin=args.margin,
            motion=args.motion,
            motion_speed=args.motion_speed,
            font_file=font_file_arg,
        )

    processed, skipped, failed = execute_plan(
        plan=plan,
        filter_arg=filter_arg,
        is_image=is_image,
        image=image_path,
        log=logger,
        encoder=args.encoder,
    )
    logger.info(
        "Done: %d processed, %d skipped, %d failed",
        processed,
        skipped,
        failed,
    )
    return EXIT_ITEM_FAILED if failed > 0 else EXIT_OK
