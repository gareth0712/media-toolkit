"""Shared ffmpeg / ffprobe binary resolution and duration probing.

Single authoritative home for the resolver logic that was previously
copy-pasted between ``videos/concat.py`` and ``videos/watermark.py``
(rule-of-three triggered when ``split.py`` joined as the third op).

Callers that need to monkeypatch the resolver cache directly (e.g.
``watermark`` tests that pin ``_RESOLVED_FFMPEG_BIN``) should continue to
patch their own module-level variables, which delegate here only on the
first call.  The cache variables here (``_RESOLVED_FFMPEG_BIN`` /
``_RESOLVED_FFPROBE_BIN``) are used exclusively by the public helpers
``resolve_ffmpeg_bin`` / ``resolve_ffprobe_bin``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Candidate binary names (same as the original concat / watermark copies).
# ---------------------------------------------------------------------------

FFMPEG_CANDIDATES: tuple[str, ...] = ("ffmpeg",)
FFPROBE_CANDIDATES: tuple[str, ...] = ("ffprobe", "ffmpeg.ffprobe")

# Conversion factor: ffprobe reports duration in fractional seconds.
_SECONDS_TO_MS = 1000

# Module-level cache — populated lazily by the public resolve helpers.
_RESOLVED_FFMPEG_BIN: str | None = None
_RESOLVED_FFPROBE_BIN: str | None = None


# ---------------------------------------------------------------------------
# Public exception.
# ---------------------------------------------------------------------------


class MissingDependencyError(Exception):
    """Raised when ffmpeg or ffprobe cannot be found on PATH.

    Inheriting from plain ``Exception`` (not a domain-specific base) keeps
    this module free of any domain dependency.  Individual ops may subclass
    it to layer in their domain hierarchy while still catching it via a
    single ``except MissingDependencyError`` at the caller.
    """


# ---------------------------------------------------------------------------
# Internal resolver (also used by concat.py's local ``_resolve_binary``
# wrapper which is imported directly by the concat tests).
# ---------------------------------------------------------------------------


def _resolve_binary(
    candidates: tuple[str, ...],
    *,
    error_class: type[MissingDependencyError] = MissingDependencyError,
) -> str:
    """Return the first executable in ``candidates`` resolvable on PATH.

    Raises:
        ``error_class``: (default: ``MissingDependencyError``) if none of the
            candidates resolve.  Callers may pass a domain-specific subclass
            so the raised type fits their own exception hierarchy.
    """
    for name in candidates:
        if shutil.which(name) is not None:
            return name
    raise error_class(
        f"none of these executables found on PATH: {', '.join(candidates)}"
    )


# ---------------------------------------------------------------------------
# Public resolution helpers.
# ---------------------------------------------------------------------------


def resolve_ffmpeg_bin() -> str:
    """Return the resolved ffmpeg binary name, cached at module level.

    Raises:
        MissingDependencyError: if none of ``FFMPEG_CANDIDATES`` resolve.
    """
    global _RESOLVED_FFMPEG_BIN
    if _RESOLVED_FFMPEG_BIN is None:
        _RESOLVED_FFMPEG_BIN = _resolve_binary(FFMPEG_CANDIDATES)
    return _RESOLVED_FFMPEG_BIN


def resolve_ffprobe_bin() -> str:
    """Return the resolved ffprobe binary name, cached at module level.

    Raises:
        MissingDependencyError: if none of ``FFPROBE_CANDIDATES`` resolve.
    """
    global _RESOLVED_FFPROBE_BIN
    if _RESOLVED_FFPROBE_BIN is None:
        _RESOLVED_FFPROBE_BIN = _resolve_binary(FFPROBE_CANDIDATES)
    return _RESOLVED_FFPROBE_BIN


# ---------------------------------------------------------------------------
# Duration probe.
# ---------------------------------------------------------------------------


def get_duration_ms(path: Path) -> int:
    """Return the duration of a media file in milliseconds via ffprobe.

    Raises:
        MissingDependencyError: if ffprobe cannot be resolved.
        RuntimeError: if ffprobe exits non-zero or returns empty output.
    """
    result = subprocess.run(
        [
            resolve_ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path}: {result.stderr.strip()}"
        )
    raw = result.stdout.strip()
    if not raw:
        raise RuntimeError(f"ffprobe returned empty duration for {path}")
    return int(float(raw) * _SECONDS_TO_MS)
