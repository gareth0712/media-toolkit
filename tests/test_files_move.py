"""Unit tests for ``media_toolkit.files.move``.

All filesystem activity stays inside ``tmp_path``. The questionary prompt
layer is intentionally out of scope (no TTY in pytest); tests that exercise
``run`` either pass ``--yes`` or provide a fully populated argparse Namespace.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pytest

from media_toolkit.files import move as move_module
from media_toolkit.files.move import (
    ACTION_MOVE,
    ACTION_OVERWRITE,
    ACTION_SKIP_BATCH_COLLISION,
    ACTION_SKIP_CONFLICT,
    ACTION_SKIP_SELF,
    EXIT_OK,
    EXIT_SETUP_ERROR,
    MovePlanEntry,
    build_move_plan,
    discover_matches,
    execute_plan,
    format_preview,
    run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_files(root: Path, names: list[str]) -> None:
    """Create empty files at ``root / name`` (creating parent dirs)."""
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")


def _silent_logger() -> logging.Logger:
    """Return a logger that doesn't propagate during tests."""
    log = logging.getLogger("test_files_move")
    log.handlers = []
    log.propagate = False
    return log


# ---------------------------------------------------------------------------
# discover_matches
# ---------------------------------------------------------------------------


def test_discover_matches_top_level_pattern(tmp_path: Path) -> None:
    _make_files(tmp_path, ["a.mp4", "b.mp4", "c.txt", "sub/d.mp4"])

    matches = discover_matches(tmp_path, "*.mp4")

    assert [p.name for p in matches] == ["a.mp4", "b.mp4"]


def test_discover_matches_recursive_pattern(tmp_path: Path) -> None:
    _make_files(tmp_path, ["a.mp4", "b.mp4", "c.txt", "sub/d.mp4"])

    matches = discover_matches(tmp_path, "**/*.mp4")

    names = sorted(p.name for p in matches)
    assert names == ["a.mp4", "b.mp4", "d.mp4"]


def test_discover_matches_skips_directories(tmp_path: Path) -> None:
    # Create a subdir that will match the glob; it must NOT be returned.
    (tmp_path / "looks_like.mp4").mkdir()
    (tmp_path / "real.mp4").write_bytes(b"")

    matches = discover_matches(tmp_path, "*.mp4")

    assert [p.name for p in matches] == ["real.mp4"]


def test_discover_matches_raises_on_missing_source(tmp_path: Path) -> None:
    missing = tmp_path / "nope"

    with pytest.raises(FileNotFoundError):
        discover_matches(missing, "*")


# ---------------------------------------------------------------------------
# build_move_plan
# ---------------------------------------------------------------------------


def test_build_move_plan_marks_move_when_no_conflict(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    _make_files(src, ["a.mp4", "b.mp4"])

    matches = discover_matches(src, "*.mp4")
    plan = build_move_plan(matches, src, dest, overwrite=False, flatten=False)

    assert [entry.action for entry in plan] == [ACTION_MOVE, ACTION_MOVE]
    assert plan[0].destination == dest / "a.mp4"
    assert plan[1].destination == dest / "b.mp4"


def test_build_move_plan_marks_skip_when_dest_exists_no_overwrite(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    _make_files(src, ["a.mp4"])
    # Pre-existing dest file -> conflict.
    (dest / "a.mp4").write_bytes(b"existing")

    matches = discover_matches(src, "*.mp4")
    plan = build_move_plan(matches, src, dest, overwrite=False, flatten=False)

    assert [entry.action for entry in plan] == [ACTION_SKIP_CONFLICT]


def test_build_move_plan_marks_overwrite_when_flag_set(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    _make_files(src, ["a.mp4"])
    (dest / "a.mp4").write_bytes(b"existing")

    matches = discover_matches(src, "*.mp4")
    plan = build_move_plan(matches, src, dest, overwrite=True, flatten=False)

    assert [entry.action for entry in plan] == [ACTION_OVERWRITE]


def test_build_move_plan_preserves_subdirs_with_recursive_pattern(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    _make_files(src, ["sub/inner/d.mp4"])

    matches = discover_matches(src, "**/*.mp4")
    plan = build_move_plan(matches, src, dest, overwrite=False, flatten=False)

    assert len(plan) == 1
    assert plan[0].destination == dest / "sub" / "inner" / "d.mp4"
    assert plan[0].action == ACTION_MOVE


# ---------------------------------------------------------------------------
# format_preview
# ---------------------------------------------------------------------------


def test_format_preview_truncates_when_over_limit(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    plan = [
        MovePlanEntry(
            source=src / f"f{i}.mp4",
            destination=dest / f"f{i}.mp4",
            action=ACTION_MOVE,
        )
        for i in range(100)
    ]

    rendered = format_preview(plan, limit=10)

    lines = rendered.splitlines()
    # 10 visible plan lines + "...and 90 more entries" + summary tail.
    assert len(lines) == 12
    assert "...and 90 more entries" in rendered
    # Zero-count categories are omitted from the tail; only "to move" survives.
    assert lines[-1] == "Total: 100 to move"


def test_format_preview_summary_counts_actions_correctly(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    plan = [
        MovePlanEntry(src / "a", dest / "a", ACTION_MOVE),
        MovePlanEntry(src / "b", dest / "b", ACTION_MOVE),
        MovePlanEntry(src / "c", dest / "c", ACTION_SKIP_CONFLICT),
        MovePlanEntry(src / "d", dest / "d", ACTION_OVERWRITE),
        MovePlanEntry(src / "e", dest / "e", ACTION_OVERWRITE),
        MovePlanEntry(src / "f", dest / "f", ACTION_OVERWRITE),
    ]

    rendered = format_preview(plan)

    # Order is fixed: move, overwrite, skip-conflict, batch-dup, skip-self.
    assert rendered.splitlines()[-1] == (
        "Total: 2 to move, 3 to overwrite, 1 to skip (conflict)"
    )


# ---------------------------------------------------------------------------
# execute_plan
# ---------------------------------------------------------------------------


def test_execute_plan_moves_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    _make_files(src, ["a.mp4", "b.mp4"])

    matches = discover_matches(src, "*.mp4")
    plan = build_move_plan(matches, src, dest, overwrite=False, flatten=False)

    moved, skipped, failed = execute_plan(plan, _silent_logger())

    assert (moved, skipped, failed) == (2, 0, 0)
    assert not (src / "a.mp4").exists()
    assert not (src / "b.mp4").exists()
    assert (dest / "a.mp4").exists()
    assert (dest / "b.mp4").exists()


def test_execute_plan_skips_conflicts(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    (src / "a.mp4").write_bytes(b"source-data")
    (dest / "a.mp4").write_bytes(b"existing-data")

    matches = discover_matches(src, "*.mp4")
    plan = build_move_plan(matches, src, dest, overwrite=False, flatten=False)

    moved, skipped, failed = execute_plan(plan, _silent_logger())

    assert (moved, skipped, failed) == (0, 1, 0)
    # Source must STILL exist (not moved).
    assert (src / "a.mp4").exists()
    assert (src / "a.mp4").read_bytes() == b"source-data"
    # Existing dest must be untouched.
    assert (dest / "a.mp4").read_bytes() == b"existing-data"


def test_execute_plan_overwrites_when_action_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    (src / "a.mp4").write_bytes(b"source-data")
    (dest / "a.mp4").write_bytes(b"existing-data")

    matches = discover_matches(src, "*.mp4")
    plan = build_move_plan(matches, src, dest, overwrite=True, flatten=False)

    moved, skipped, failed = execute_plan(plan, _silent_logger())

    assert (moved, skipped, failed) == (1, 0, 0)
    assert not (src / "a.mp4").exists()
    assert (dest / "a.mp4").read_bytes() == b"source-data"


def test_execute_plan_creates_dest_parent_dirs(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    # Deliberately do NOT create dest or any subdir.
    _make_files(src, ["sub/inner/d.mp4"])

    matches = discover_matches(src, "**/*.mp4")
    plan = build_move_plan(matches, src, dest, overwrite=False, flatten=False)

    moved, skipped, failed = execute_plan(plan, _silent_logger())

    assert (moved, skipped, failed) == (1, 0, 0)
    assert (dest / "sub" / "inner" / "d.mp4").exists()


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_returns_setup_error_on_missing_source_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    args = argparse.Namespace(
        source_dir=str(missing),
        dest_dir=str(tmp_path / "out"),
        pattern="*",
        overwrite=False,
        flatten=False,
        yes=True,
        quiet=True,
    )

    rc = run(args)

    assert rc == EXIT_SETUP_ERROR


def test_run_returns_zero_on_no_matches(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()

    args = argparse.Namespace(
        source_dir=str(src),
        dest_dir=str(dest),
        pattern="*.mp4",
        overwrite=False,
        flatten=False,
        yes=True,
        quiet=True,
    )

    rc = run(args)

    assert rc == EXIT_OK


def test_run_with_yes_flag_executes_without_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    _make_files(src, ["a.mp4"])

    # Monkeypatch questionary so any accidental prompt fails the test loudly.
    import questionary

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("questionary should not be called when --yes is set")

    monkeypatch.setattr(questionary, "confirm", boom)
    monkeypatch.setattr(questionary, "path", boom)
    monkeypatch.setattr(questionary, "text", boom)

    args = argparse.Namespace(
        source_dir=str(src),
        dest_dir=str(dest),
        pattern="*.mp4",
        overwrite=False,
        flatten=False,
        yes=True,
        quiet=True,
    )

    rc = run(args)

    assert rc == EXIT_OK
    assert not (src / "a.mp4").exists()
    assert (dest / "a.mp4").exists()


# ---------------------------------------------------------------------------
# Module surface sanity check
# ---------------------------------------------------------------------------


def test_module_exposes_required_op_attributes() -> None:
    assert move_module.NAME == "move"
    assert move_module.DESCRIPTION
    assert callable(move_module.register_subparser)
    assert callable(move_module.run)
    assert callable(move_module.interactive_args)


# ---------------------------------------------------------------------------
# build_move_plan -- flatten + new conflict types
# ---------------------------------------------------------------------------


def test_build_move_plan_flatten_uses_basename(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    _make_files(src, ["sub/inner/d.mp4", "other/e.mp4"])

    matches = discover_matches(src, "**/*.mp4")
    plan = build_move_plan(matches, src, dest, overwrite=False, flatten=True)

    # Destinations are dest_dir / basename -- no subdir mirror.
    destinations = sorted(entry.destination for entry in plan)
    assert destinations == [dest / "d.mp4", dest / "e.mp4"]
    assert all(entry.action == ACTION_MOVE for entry in plan)


def test_build_move_plan_no_flatten_preserves_subdirs(tmp_path: Path) -> None:
    # Regression: flatten=False must reproduce the existing subdir behavior.
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    _make_files(src, ["sub/inner/d.mp4", "other/e.mp4"])

    matches = discover_matches(src, "**/*.mp4")
    plan = build_move_plan(matches, src, dest, overwrite=False, flatten=False)

    destinations = sorted(entry.destination for entry in plan)
    assert destinations == [
        dest / "other" / "e.mp4",
        dest / "sub" / "inner" / "d.mp4",
    ]
    assert all(entry.action == ACTION_MOVE for entry in plan)


def test_build_move_plan_detects_skip_self(tmp_path: Path) -> None:
    # Top-level match with src == dest naturally maps source path to itself.
    shared = tmp_path / "shared"
    shared.mkdir()
    _make_files(shared, ["a.mp4"])

    matches = discover_matches(shared, "*.mp4")
    plan = build_move_plan(matches, shared, shared, overwrite=False, flatten=False)

    assert len(plan) == 1
    assert plan[0].action == ACTION_SKIP_SELF
    assert plan[0].source == plan[0].destination


def test_build_move_plan_detects_intra_batch_collision_with_flatten(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    # Two distinct source files that share a basename.
    _make_files(src, ["sub1/dup.mp4", "sub2/dup.mp4"])

    matches = discover_matches(src, "**/*.mp4")
    plan = build_move_plan(matches, src, dest, overwrite=False, flatten=True)

    actions = [entry.action for entry in plan]
    # First entry (sorted order) wins; the rest become batch collisions.
    assert actions == [ACTION_MOVE, ACTION_SKIP_BATCH_COLLISION]
    # Both entries target the same flattened destination.
    assert plan[0].destination == dest / "dup.mp4"
    assert plan[1].destination == dest / "dup.mp4"


def test_build_move_plan_intra_batch_collision_ignores_overwrite_flag(
    tmp_path: Path,
) -> None:
    # Even with --overwrite, intra-batch collisions stay skipped (footgun guard).
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    _make_files(src, ["sub1/dup.mp4", "sub2/dup.mp4"])

    matches = discover_matches(src, "**/*.mp4")
    plan = build_move_plan(matches, src, dest, overwrite=True, flatten=True)

    assert [entry.action for entry in plan] == [
        ACTION_MOVE,
        ACTION_SKIP_BATCH_COLLISION,
    ]


# ---------------------------------------------------------------------------
# format_preview -- new categories in summary tail
# ---------------------------------------------------------------------------


def test_format_preview_shows_skip_self_in_summary(tmp_path: Path) -> None:
    src = tmp_path / "src"
    plan = [
        MovePlanEntry(src / "a.mp4", src / "a.mp4", ACTION_SKIP_SELF),
    ]

    rendered = format_preview(plan)

    tail = rendered.splitlines()[-1]
    assert "1 already at destination" in tail


def test_format_preview_shows_batch_collision_in_summary(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    plan = [
        MovePlanEntry(src / "sub1/dup.mp4", dest / "dup.mp4", ACTION_MOVE),
        MovePlanEntry(
            src / "sub2/dup.mp4",
            dest / "dup.mp4",
            ACTION_SKIP_BATCH_COLLISION,
        ),
    ]

    rendered = format_preview(plan)

    tail = rendered.splitlines()[-1]
    assert "1 to skip (batch dup)" in tail


def test_format_preview_omits_zero_counts_from_summary(tmp_path: Path) -> None:
    # All-moves plan: tail should NOT mention skip / overwrite / batch / self.
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    plan = [
        MovePlanEntry(src / "a", dest / "a", ACTION_MOVE),
        MovePlanEntry(src / "b", dest / "b", ACTION_MOVE),
    ]

    rendered = format_preview(plan)

    tail = rendered.splitlines()[-1]
    assert tail == "Total: 2 to move"
    assert "skip" not in tail
    assert "overwrite" not in tail
    assert "already" not in tail


# ---------------------------------------------------------------------------
# execute_plan -- new skip types
# ---------------------------------------------------------------------------


def test_execute_plan_skips_self_entries_quietly(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "a.mp4").write_bytes(b"data")

    matches = discover_matches(shared, "*.mp4")
    plan = build_move_plan(matches, shared, shared, overwrite=False, flatten=False)

    moved, skipped, failed = execute_plan(plan, _silent_logger())

    assert (moved, skipped, failed) == (0, 1, 0)
    # File untouched at its original location.
    assert (shared / "a.mp4").read_bytes() == b"data"


def test_execute_plan_skips_batch_collision_entries(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    # Manually craft a single batch-collision entry to isolate the skip path.
    plan = [
        MovePlanEntry(
            source=src / "sub2/dup.mp4",
            destination=dest / "dup.mp4",
            action=ACTION_SKIP_BATCH_COLLISION,
        ),
    ]

    moved, skipped, failed = execute_plan(plan, _silent_logger())

    assert (moved, skipped, failed) == (0, 1, 0)


# ---------------------------------------------------------------------------
# run -- end-to-end with --flatten
# ---------------------------------------------------------------------------


def test_run_with_flatten_moves_nested_files_to_top(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dst"
    src.mkdir()
    dest.mkdir()
    _make_files(src, ["sub/a.mp4", "sub/b.mp4"])

    args = argparse.Namespace(
        source_dir=str(src),
        dest_dir=str(dest),
        pattern="**/*.mp4",
        overwrite=False,
        flatten=True,
        yes=True,
        quiet=True,
    )

    rc = run(args)

    assert rc == EXIT_OK
    # Files end up at top level of dest, NOT under dest/sub/.
    assert (dest / "a.mp4").exists()
    assert (dest / "b.mp4").exists()
    assert not (dest / "sub").exists()
    # Source files are gone.
    assert not (src / "sub" / "a.mp4").exists()
    assert not (src / "sub" / "b.mp4").exists()
