"""Bulk-move files matching a glob pattern from one directory to another.

The op discovers files in ``source_dir`` that match a user-supplied glob
pattern, presents a preview of every planned move (with conflict /
overwrite annotations), then prompts for confirmation before performing
the actual ``shutil.move`` calls. Designed to be safe by default: existing
files at the destination are skipped unless ``--overwrite`` is set.

Pure logic helpers (``discover_matches``, ``build_move_plan``,
``format_preview``) are factored out so they can be unit-tested without
touching the filesystem beyond ``tmp_path``.
"""

from __future__ import annotations

import argparse
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from media_toolkit.path_utils import normalize_path_input

logger = logging.getLogger(__name__)

NAME = "move"
DESCRIPTION = (
    "Bulk-move files matching a glob pattern from one directory to another."
)

# Exit codes (mirrors HANDOFF Section 7).
EXIT_OK = 0
EXIT_ITEM_FAILED = 1
EXIT_SETUP_ERROR = 2
EXIT_USER_ABORT = 130

# Preview truncation: if more matches than this, show first N + tail summary.
PREVIEW_LIMIT = 50

# Plan-entry action labels (no magic strings).
ACTION_MOVE = "move"
ACTION_SKIP_CONFLICT = "skip-conflict"
ACTION_OVERWRITE = "overwrite"
ACTION_SKIP_SELF = "skip-self"
ACTION_SKIP_BATCH_COLLISION = "skip-batch-collision"

# Actions that should be silently skipped during execution (no shutil.move call).
SKIP_ACTIONS = (
    ACTION_SKIP_CONFLICT,
    ACTION_SKIP_SELF,
    ACTION_SKIP_BATCH_COLLISION,
)

# Default glob pattern when the user accepts the prompt default.
DEFAULT_PATTERN = "*"


@dataclass(frozen=True)
class MovePlanEntry:
    """One planned move: source path, destination path, and action label."""

    source: Path
    destination: Path
    action: str  # one of ACTION_MOVE / ACTION_SKIP_CONFLICT / ACTION_OVERWRITE


# ---------------------------------------------------------------------------
# Pure logic (no filesystem mutation; tested directly).
# ---------------------------------------------------------------------------


def discover_matches(source_dir: Path, pattern: str) -> list[Path]:
    """Return a sorted list of files (not directories) matching the glob pattern.

    Raises:
        FileNotFoundError: if ``source_dir`` does not exist or is not a directory.
    """
    if not source_dir.is_dir():
        raise FileNotFoundError(
            f"source directory does not exist or is not a directory: {source_dir}"
        )
    return sorted(p for p in source_dir.glob(pattern) if p.is_file())


def build_move_plan(
    matches: list[Path],
    source_dir: Path,
    dest_dir: Path,
    overwrite: bool,
    flatten: bool,
) -> list[MovePlanEntry]:
    """Build a per-file move plan.

    When ``flatten`` is False (default), each entry's destination is
    ``dest_dir / match.relative_to(source_dir)``, which reproduces sub-tree
    layout when a recursive ``**/*.ext`` pattern is used and degenerates to
    ``dest_dir / filename`` for a flat ``*.ext`` glob.

    When ``flatten`` is True, each entry's destination is
    ``dest_dir / match.name`` -- nested matches get collapsed into the
    destination directory using basename only (no subdir mirror).

    Action assignment (in priority order):
        * ``ACTION_SKIP_SELF`` if source path == destination path.
        * ``ACTION_SKIP_BATCH_COLLISION`` if another earlier match in this
          batch already targets the same destination (overwrite flag is
          intentionally ignored here -- intra-batch overwrite is a footgun).
        * ``ACTION_OVERWRITE`` if the destination exists and ``overwrite`` is True.
        * ``ACTION_SKIP_CONFLICT`` if the destination exists and ``overwrite`` is False.
        * ``ACTION_MOVE`` otherwise.
    """
    plan: list[MovePlanEntry] = []
    planned_dests: set[Path] = set()
    for match in matches:
        if flatten:
            destination = dest_dir / match.name
        else:
            destination = dest_dir / match.relative_to(source_dir)

        if destination == match:
            action = ACTION_SKIP_SELF
        elif destination in planned_dests:
            action = ACTION_SKIP_BATCH_COLLISION
        elif destination.exists():
            action = ACTION_OVERWRITE if overwrite else ACTION_SKIP_CONFLICT
        else:
            action = ACTION_MOVE

        plan.append(
            MovePlanEntry(source=match, destination=destination, action=action)
        )
        planned_dests.add(destination)
    return plan


def _format_plan_line(entry: MovePlanEntry) -> str:
    """Render one plan entry as a human-readable preview line."""
    if entry.action == ACTION_SKIP_SELF:
        return f"skip-self  {entry.source.name} (already at destination)"
    if entry.action == ACTION_SKIP_BATCH_COLLISION:
        return (
            f"skip-dup   {entry.source.name} -> {entry.destination} "
            f"(another file in this batch already targets that path)"
        )
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
    return f"move       {entry.source.name} -> {entry.destination}"


def format_preview(plan: list[MovePlanEntry], limit: int = PREVIEW_LIMIT) -> str:
    """Build a human-readable preview block plus a summary tail.

    If ``len(plan) > limit``, only the first ``limit`` lines are emitted,
    followed by ``...and X more entries`` and the summary line.

    The summary line always lists the move count, then appends only the
    non-zero categories among overwrite / skip-conflict / batch-collision
    / skip-self -- this keeps the tail short for the common all-moves case.
    """
    counts: dict[str, int] = {
        ACTION_MOVE: 0,
        ACTION_SKIP_CONFLICT: 0,
        ACTION_OVERWRITE: 0,
        ACTION_SKIP_SELF: 0,
        ACTION_SKIP_BATCH_COLLISION: 0,
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

    summary_parts: list[str] = [f"{counts[ACTION_MOVE]} to move"]
    if counts[ACTION_OVERWRITE]:
        summary_parts.append(f"{counts[ACTION_OVERWRITE]} to overwrite")
    if counts[ACTION_SKIP_CONFLICT]:
        summary_parts.append(f"{counts[ACTION_SKIP_CONFLICT]} to skip (conflict)")
    if counts[ACTION_SKIP_BATCH_COLLISION]:
        summary_parts.append(
            f"{counts[ACTION_SKIP_BATCH_COLLISION]} to skip (batch dup)"
        )
    if counts[ACTION_SKIP_SELF]:
        summary_parts.append(
            f"{counts[ACTION_SKIP_SELF]} already at destination"
        )
    lines.append("Total: " + ", ".join(summary_parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# I/O wrappers (touch the filesystem).
# ---------------------------------------------------------------------------


def execute_plan(
    plan: list[MovePlanEntry], log: logging.Logger
) -> tuple[int, int, int]:
    """Execute the plan via ``shutil.move``. Returns (moved, skipped, failed).

    Per-entry semantics:
        * ``ACTION_SKIP_CONFLICT``: log and increment ``skipped`` counter.
        * ``ACTION_MOVE`` / ``ACTION_OVERWRITE``: ensure parent dir exists,
          then ``shutil.move``. On exception, log the error and increment
          ``failed`` (the batch continues with the next entry).
    """
    moved = 0
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
            shutil.move(str(entry.source), str(entry.destination))
            log.info("moved: %s -> %s", entry.source, entry.destination)
            moved += 1
        except OSError as exc:
            log.error(
                "failed to move %s -> %s: %s",
                entry.source,
                entry.destination,
                exc,
            )
            failed += 1
    return moved, skipped, failed


# ---------------------------------------------------------------------------
# CLI integration.
# ---------------------------------------------------------------------------


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the move subcommand under the files domain parser."""
    parser = subparsers.add_parser(NAME, help=DESCRIPTION)
    parser.add_argument(
        "--source-dir",
        required=False,
        default=None,
        help="Directory to move files FROM.",
    )
    parser.add_argument(
        "--dest-dir",
        required=False,
        default=None,
        help="Directory to move files TO.",
    )
    parser.add_argument(
        "--pattern",
        required=False,
        default=None,
        help=(
            "Glob pattern. '*.mp4' matches top-level only; "
            "'**/*.mp4' recurses into subdirectories. "
            "Use --flatten to drop recursive matches into dest-dir without subdir mirror."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=None,
        help="Overwrite existing files at the destination (default: skip conflicts).",
    )
    parser.add_argument(
        "--flatten",
        action="store_true",
        default=None,
        help=(
            "Drop matched files directly into --dest-dir using basename only "
            "(no subdir mirror). Use with recursive patterns like '**/*.mp4'."
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
        help="Suppress per-file progress output (file log unaffected).",
    )
    parser.set_defaults(func=run)


def interactive_args(
    prefilled: argparse.Namespace | None = None,
) -> argparse.Namespace:
    """Fill any missing move args via questionary prompts.

    A field is treated as "user did not supply" when it is ``None`` on
    ``prefilled``. ``overwrite`` is only prompted in interactive mode (i.e.
    at least one of source-dir / dest-dir was missing); when both are
    pre-supplied (scripted invocation), unset ``overwrite`` defaults to
    False. ``yes`` and ``quiet`` are never prompted.
    """
    import questionary

    base = prefilled if prefilled is not None else argparse.Namespace()

    source_dir = getattr(base, "source_dir", None)
    dest_dir = getattr(base, "dest_dir", None)
    pattern = getattr(base, "pattern", None)
    overwrite = getattr(base, "overwrite", None)
    flatten = getattr(base, "flatten", None)

    interactive_mode = source_dir is None or dest_dir is None

    if source_dir is None:
        source_dir = questionary.path("Source directory:").ask()

    if dest_dir is None:
        dest_dir = questionary.path("Destination directory:").ask()

    if pattern is None:
        pattern = questionary.text(
            "Glob pattern (e.g. '*.mp4' for top-level only, '**/*.mp4' for recursive):",
            default=DEFAULT_PATTERN,
        ).ask()

    if overwrite is None:
        if interactive_mode:
            overwrite = questionary.confirm(
                "Overwrite existing files at destination?", default=False
            ).ask()
        else:
            overwrite = False

    if flatten is None:
        if interactive_mode:
            flatten = questionary.confirm(
                "Flatten subdirectories (drop everything directly into dest)?",
                default=False,
            ).ask()
        else:
            flatten = False

    yes = bool(getattr(base, "yes", False))
    quiet = bool(getattr(base, "quiet", False))

    return argparse.Namespace(
        source_dir=source_dir,
        dest_dir=dest_dir,
        pattern=pattern,
        overwrite=bool(overwrite) if overwrite is not None else None,
        flatten=bool(flatten) if flatten is not None else None,
        yes=yes,
        quiet=quiet,
    )


def run(args: argparse.Namespace) -> int:
    """Execute the move op. Returns an exit code per HANDOFF Section 7."""
    args = interactive_args(prefilled=args)

    # Detect user abort (Ctrl-C inside a questionary prompt yields None).
    if (
        args.source_dir is None
        or args.dest_dir is None
        or args.pattern is None
        or args.overwrite is None
        or args.flatten is None
    ):
        logger.error("error: aborted by user")
        return EXIT_USER_ABORT

    source_dir = normalize_path_input(args.source_dir).expanduser().resolve()
    dest_dir = normalize_path_input(args.dest_dir).expanduser().resolve()
    pattern = args.pattern

    try:
        matches = discover_matches(source_dir, pattern)
    except FileNotFoundError as exc:
        logger.error("error: %s", exc)
        return EXIT_SETUP_ERROR

    if not matches:
        logger.warning(
            "no files matched pattern %r in %s; nothing to do", pattern, source_dir
        )
        return EXIT_OK

    plan = build_move_plan(
        matches=matches,
        source_dir=source_dir,
        dest_dir=dest_dir,
        overwrite=bool(args.overwrite),
        flatten=bool(args.flatten),
    )

    logger.info("Move preview:\n%s", format_preview(plan))

    if not args.yes:
        import questionary

        confirmed = questionary.confirm(
            f"Proceed with {len(plan)} entries?", default=False
        ).ask()
        if confirmed is None:
            logger.warning("aborted by user")
            return EXIT_USER_ABORT
        if not confirmed:
            logger.info("user declined; no files moved")
            return EXIT_OK

    dest_dir.mkdir(parents=True, exist_ok=True)

    moved, skipped, failed = execute_plan(plan, logger)
    logger.info(
        "Done: %d moved, %d skipped, %d failed", moved, skipped, failed
    )
    return EXIT_ITEM_FAILED if failed > 0 else EXIT_OK
