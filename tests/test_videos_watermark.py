"""Unit tests for ``media_toolkit.videos.watermark``.

All tests stay pure: the ffmpeg / ffprobe layer is exercised via
``monkeypatch`` over ``subprocess.run`` so no real binary invocations
happen. Filesystem activity is confined to ``tmp_path`` fixtures.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from pathlib import Path

import pytest

from media_toolkit.videos import watermark as watermark_module
from media_toolkit.videos.watermark import (
    ACTION_OVERWRITE,
    ACTION_PROCESS,
    ACTION_SKIP_CONFLICT,
    BOUNCE_SPEED_X_PX_PER_SEC,
    BOUNCE_SPEED_Y_PX_PER_SEC,
    DEFAULT_CJK_FONT_FILE,
    DEFAULT_ENCODER,
    DEFAULT_FONT_COLOR,
    DEFAULT_FONT_FILE,
    DEFAULT_FONT_SIZE,
    DEFAULT_IMAGE_SCALE,
    DEFAULT_MARGIN_PX,
    DEFAULT_MOTION,
    DEFAULT_MOTION_SPEED,
    DEFAULT_OPACITY,
    DEFAULT_POSITION,
    DRIFT_FREQ_X,
    DRIFT_FREQ_Y,
    ENCODER_CPU,
    ENCODER_GPU,
    EXIT_OK,
    EXIT_SETUP_ERROR,
    MOTION_BOUNCE,
    MOTION_DRIFT,
    MOTION_STATIC,
    POSITION_BOTTOM_LEFT,
    POSITION_BOTTOM_RIGHT,
    POSITION_CENTER,
    POSITION_TOP_LEFT,
    POSITION_TOP_RIGHT,
    WatermarkPlanEntry,
    _build_video_codec_args,
    _escape_ffmpeg_expr,
    apply_watermark_to_video,
    build_drawtext_filter,
    build_overlay_filter,
    build_watermark_plan,
    discover_videos,
    format_preview,
    has_cjk_chars,
    motion_to_xy_expressions,
    position_to_xy_expressions,
    run,
    select_font_file,
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
    log = logging.getLogger("test_videos_watermark")
    log.handlers = []
    log.propagate = False
    return log


# ---------------------------------------------------------------------------
# position_to_xy_expressions
# ---------------------------------------------------------------------------


def test_position_to_xy_image_top_left() -> None:
    x, y = position_to_xy_expressions(POSITION_TOP_LEFT, 20, is_text=False)
    assert (x, y) == ("20", "20")


def test_position_to_xy_image_bottom_right() -> None:
    x, y = position_to_xy_expressions(POSITION_BOTTOM_RIGHT, 20, is_text=False)
    assert (x, y) == ("W-w-20", "H-h-20")


def test_position_to_xy_text_uses_tw_th() -> None:
    x, y = position_to_xy_expressions(POSITION_BOTTOM_RIGHT, 10, is_text=True)
    assert "tw" in x
    assert "th" in y
    # And critically NOT the overlay vars.
    assert "-w-" not in x  # would be "W-w-10" if image vars leaked through
    assert "-h-" not in y


def test_position_to_xy_text_top_right_uses_tw() -> None:
    x, y = position_to_xy_expressions(POSITION_TOP_RIGHT, 5, is_text=True)
    assert x == "W-tw-5"
    assert y == "5"


def test_position_to_xy_text_bottom_left_uses_th() -> None:
    x, y = position_to_xy_expressions(POSITION_BOTTOM_LEFT, 5, is_text=True)
    assert x == "5"
    assert y == "H-th-5"


def test_position_to_xy_center_image_mode() -> None:
    x, y = position_to_xy_expressions(POSITION_CENTER, 0, is_text=False)
    assert x == "(W-w)/2"
    assert y == "(H-h)/2"


def test_position_to_xy_center_text_mode() -> None:
    x, y = position_to_xy_expressions(POSITION_CENTER, 0, is_text=True)
    assert x == "(W-tw)/2"
    assert y == "(H-th)/2"


def test_position_to_xy_unknown_preset_raises() -> None:
    with pytest.raises(ValueError):
        position_to_xy_expressions("middle", 10, is_text=False)


# ---------------------------------------------------------------------------
# motion_to_xy_expressions
# ---------------------------------------------------------------------------


def test_motion_static_raises() -> None:
    with pytest.raises(ValueError):
        motion_to_xy_expressions(MOTION_STATIC, 1.0, is_text=False)


def test_motion_bounce_uses_t() -> None:
    x, y = motion_to_xy_expressions(MOTION_BOUNCE, 1.0, is_text=False)
    assert "t" in x
    assert "t" in y
    # Bounce uses an absolute-value triangle wave -> abs() in expression.
    assert x.startswith("abs(")
    assert y.startswith("abs(")


def test_motion_bounce_speed_multiplier() -> None:
    x_one, _ = motion_to_xy_expressions(MOTION_BOUNCE, 1.0, is_text=False)
    x_two, _ = motion_to_xy_expressions(MOTION_BOUNCE, 2.0, is_text=False)
    base_speed_str = str(BOUNCE_SPEED_X_PX_PER_SEC * 1.0)
    doubled_str = str(BOUNCE_SPEED_X_PX_PER_SEC * 2.0)
    assert base_speed_str in x_one
    assert doubled_str in x_two
    assert doubled_str not in x_one


def test_motion_bounce_y_uses_y_speed() -> None:
    _, y = motion_to_xy_expressions(MOTION_BOUNCE, 1.0, is_text=False)
    assert str(BOUNCE_SPEED_Y_PX_PER_SEC * 1.0) in y


def test_motion_drift_uses_t_and_freqs() -> None:
    x, y = motion_to_xy_expressions(MOTION_DRIFT, 1.0, is_text=False)
    assert "t" in x and "t" in y
    assert str(DRIFT_FREQ_X * 1.0) in x
    assert str(DRIFT_FREQ_Y * 1.0) in y
    # Drift uses mod() to keep wm in-bounds.
    assert "mod(" in x
    assert "mod(" in y


def test_motion_drift_text_mode_uses_tw_th() -> None:
    x, y = motion_to_xy_expressions(MOTION_DRIFT, 1.0, is_text=True)
    assert "tw" in x
    assert "th" in y


def test_motion_unknown_mode_raises() -> None:
    with pytest.raises(ValueError):
        motion_to_xy_expressions("zigzag", 1.0, is_text=False)


# ---------------------------------------------------------------------------
# build_drawtext_filter
# ---------------------------------------------------------------------------


def test_build_drawtext_filter_escapes_special_chars() -> None:
    # All four special chars in one go: backslash, single-quote, colon, percent.
    rendered = build_drawtext_filter(
        text="a\\b'c:d%e",
        font_size=24,
        font_color=DEFAULT_FONT_COLOR,
        opacity=0.5,
        position=POSITION_BOTTOM_RIGHT,
        margin=10,
        motion=MOTION_STATIC,
        motion_speed=1.0,
    )
    # Backslash doubled.
    assert "a\\\\b" in rendered
    # Single quote turned into close-quote escaped-quote re-open: '\''
    assert "'\\''" in rendered
    # Colon escaped.
    assert "\\:" in rendered
    # Percent escaped.
    assert "\\%" in rendered


def test_build_drawtext_filter_includes_opacity_in_color() -> None:
    rendered = build_drawtext_filter(
        text="hello",
        font_size=24,
        font_color="white",
        opacity=0.5,
        position=POSITION_BOTTOM_RIGHT,
        margin=20,
        motion=MOTION_STATIC,
        motion_speed=1.0,
    )
    assert "fontcolor=white@0.5" in rendered


def test_build_drawtext_filter_static_uses_position_expr() -> None:
    rendered = build_drawtext_filter(
        text="hi",
        font_size=24,
        font_color="white",
        opacity=1.0,
        position=POSITION_BOTTOM_RIGHT,
        margin=20,
        motion=MOTION_STATIC,
        motion_speed=1.0,
    )
    assert "x=W-tw-20" in rendered
    assert "y=H-th-20" in rendered


def test_build_drawtext_filter_bounce_uses_motion_expr() -> None:
    rendered = build_drawtext_filter(
        text="hi",
        font_size=24,
        font_color="white",
        opacity=1.0,
        position=POSITION_BOTTOM_RIGHT,
        margin=20,
        motion=MOTION_BOUNCE,
        motion_speed=1.0,
    )
    # Bounce expressions reference t and use abs(...).
    assert "x=abs(" in rendered
    assert "y=abs(" in rendered
    assert "*t" in rendered


# ---------------------------------------------------------------------------
# build_overlay_filter
# ---------------------------------------------------------------------------


def test_build_overlay_filter_includes_image_and_alpha(tmp_path: Path) -> None:
    image = tmp_path / "wm.png"
    image.write_bytes(b"")
    rendered = build_overlay_filter(
        image_path=image,
        opacity=0.5,
        scale_pixels_w=320,
        position=POSITION_BOTTOM_RIGHT,
        margin=20,
        motion=MOTION_STATIC,
        motion_speed=1.0,
    )
    # Alpha multiplier baked in via colorchannelmixer.
    assert "colorchannelmixer=aa=0.5" in rendered
    # Watermark scale (rounded down to even).
    assert "scale=320:-2" in rendered
    # The overlay step references the [wm] label and the main video.
    assert "[wm]" in rendered
    assert "overlay=" in rendered


def test_build_overlay_filter_bounce_motion() -> None:
    rendered = build_overlay_filter(
        image_path=Path("/tmp/wm.png"),
        opacity=1.0,
        scale_pixels_w=300,
        position=POSITION_BOTTOM_RIGHT,
        margin=20,
        motion=MOTION_BOUNCE,
        motion_speed=2.0,
    )
    # Bounce uses abs() and references t.
    assert "abs(" in rendered
    assert "*t" in rendered


def test_build_overlay_filter_rounds_to_even_width() -> None:
    rendered = build_overlay_filter(
        image_path=Path("/tmp/wm.png"),
        opacity=1.0,
        scale_pixels_w=321,  # odd
        position=POSITION_TOP_LEFT,
        margin=10,
        motion=MOTION_STATIC,
        motion_speed=1.0,
    )
    # 321 -> 320 (rounded down to even for libx264).
    assert "scale=320:-2" in rendered


# ---------------------------------------------------------------------------
# _escape_ffmpeg_expr — option-value escaping for x/y / fontfile interpolation
# ---------------------------------------------------------------------------


def test_escape_ffmpeg_expr_handles_comma() -> None:
    # Commas inside motion expressions must be backslash-escaped, otherwise
    # ffmpeg treats the expression as a filter-chain separator.
    assert _escape_ffmpeg_expr("mod(80*t, 2*W)") == "mod(80*t\\, 2*W)"


def test_escape_ffmpeg_expr_handles_colon() -> None:
    # Colons separate option=value pairs in ffmpeg, so they must be escaped
    # when they appear inside an option value (e.g. a Windows-style font path).
    assert _escape_ffmpeg_expr("D:/fonts/x.ttf") == "D\\:/fonts/x.ttf"


def test_escape_ffmpeg_expr_handles_backslash() -> None:
    # Backslashes must be doubled so ffmpeg sees a single literal backslash
    # after its own one round of unescaping.
    assert _escape_ffmpeg_expr("a\\b") == "a\\\\b"


def test_escape_ffmpeg_expr_handles_all_three() -> None:
    # Combined: backslash escaped first so we don't double-process commas/colons
    # we just emitted.
    assert _escape_ffmpeg_expr("a\\b,c:d") == "a\\\\b\\,c\\:d"


def test_build_drawtext_filter_escapes_motion_expression() -> None:
    rendered = build_drawtext_filter(
        text="hi",
        font_size=24,
        font_color="white",
        opacity=1.0,
        position=POSITION_BOTTOM_RIGHT,
        margin=20,
        motion=MOTION_BOUNCE,
        motion_speed=1.0,
    )
    # Bounce x-expr is abs(mod(80.0*t,2*(W-tw))-(W-tw)). The raw ',' would
    # be a filter-chain separator; we expect it escaped to '\,'.
    assert "\\," in rendered
    # The unescaped form must NOT appear -- specifically the ',' immediately
    # after the '*t' inside mod(...).
    assert "*t,2*(W" not in rendered
    assert "*t\\,2*(W" in rendered


def test_build_overlay_filter_escapes_motion_expression() -> None:
    rendered = build_overlay_filter(
        image_path=Path("/tmp/wm.png"),
        opacity=1.0,
        scale_pixels_w=300,
        position=POSITION_BOTTOM_RIGHT,
        margin=20,
        motion=MOTION_BOUNCE,
        motion_speed=1.0,
    )
    # Same escaping requirement applies to the overlay= option value.
    # NB: the head ", colorchannelmixer=aa=...," and ",scale=...," commas
    # belong to the filter chain itself, not an option value, so they stay
    # raw. The escaped comma we care about is the one inside abs(mod(*t, ...)).
    assert "*t\\,2*(W-w)" in rendered


def test_build_drawtext_filter_includes_fontfile() -> None:
    # By default the filter must inject fontfile=<DEFAULT_FONT_FILE> so we
    # bypass fontconfig (broken under snap-confined ffmpeg).
    rendered = build_drawtext_filter(
        text="hi",
        font_size=24,
        font_color="white",
        opacity=1.0,
        position=POSITION_BOTTOM_RIGHT,
        margin=20,
        motion=MOTION_STATIC,
        motion_speed=1.0,
    )
    # The default path has no special chars, so it appears verbatim.
    assert f"fontfile={DEFAULT_FONT_FILE}" in rendered


def test_build_drawtext_filter_uses_custom_fontfile() -> None:
    # When the caller passes a custom path, that path is used and it sits
    # before text= in the option list.
    custom = "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"
    rendered = build_drawtext_filter(
        text="hi",
        font_size=24,
        font_color="white",
        opacity=1.0,
        position=POSITION_BOTTOM_RIGHT,
        margin=20,
        motion=MOTION_STATIC,
        motion_speed=1.0,
        font_file=custom,
    )
    assert f"fontfile={custom}" in rendered
    # Sanity: the default path should NOT appear when an override was given.
    assert DEFAULT_FONT_FILE not in rendered


# ---------------------------------------------------------------------------
# discover_videos
# ---------------------------------------------------------------------------


def test_discover_videos_single_file(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")

    # Pattern is irrelevant when input is a file.
    matches = discover_videos(video, "*.mkv")

    assert matches == [video]


def test_discover_videos_single_file_rejects_non_video_ext(tmp_path: Path) -> None:
    bogus = tmp_path / "notes.txt"
    bogus.write_bytes(b"")

    with pytest.raises(ValueError):
        discover_videos(bogus, "*")


def test_discover_videos_directory_top_level_pattern(tmp_path: Path) -> None:
    _make_files(
        tmp_path,
        ["a.mp4", "b.mp4", "ignore.srt", "notes.txt", "sub/inner.mp4"],
    )

    matches = discover_videos(tmp_path, "*.mp4")

    assert [p.name for p in matches] == ["a.mp4", "b.mp4"]


def test_discover_videos_directory_recursive_pattern(tmp_path: Path) -> None:
    _make_files(tmp_path, ["a.mp4", "sub/inner.mp4", "sub/deeper/d.mp4"])

    matches = discover_videos(tmp_path, "**/*.mp4")

    names = sorted(p.name for p in matches)
    assert names == ["a.mp4", "d.mp4", "inner.mp4"]


def test_discover_videos_missing_input_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(FileNotFoundError):
        discover_videos(missing, "*.mp4")


# ---------------------------------------------------------------------------
# build_watermark_plan
# ---------------------------------------------------------------------------


def test_build_watermark_plan_file_to_file(tmp_path: Path) -> None:
    src = tmp_path / "in.mp4"
    src.write_bytes(b"")
    dest = tmp_path / "out.mp4"

    plan = build_watermark_plan(
        videos=[src],
        input_path=src,
        output_path=dest,
        overwrite=False,
    )

    assert len(plan) == 1
    assert plan[0].source == src
    assert plan[0].destination == dest
    assert plan[0].action == ACTION_PROCESS


def test_build_watermark_plan_dir_to_dir_no_subdir_mirror(tmp_path: Path) -> None:
    src_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    src_dir.mkdir()
    out_dir.mkdir()
    nested = src_dir / "sub" / "inner.mp4"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_bytes(b"")
    flat = src_dir / "top.mp4"
    flat.write_bytes(b"")

    plan = build_watermark_plan(
        videos=[flat, nested],
        input_path=src_dir,
        output_path=out_dir,
        overwrite=False,
    )

    # Both destinations are basename-only under out_dir (no subdir mirror).
    assert plan[0].destination == out_dir / "top.mp4"
    assert plan[1].destination == out_dir / "inner.mp4"
    assert plan[0].action == ACTION_PROCESS
    assert plan[1].action == ACTION_PROCESS


def test_build_watermark_plan_marks_skip_conflict(tmp_path: Path) -> None:
    src_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    src_dir.mkdir()
    out_dir.mkdir()
    video = src_dir / "a.mp4"
    video.write_bytes(b"")
    # Pre-existing dest file with same name -> conflict.
    (out_dir / "a.mp4").write_bytes(b"existing")

    plan = build_watermark_plan(
        videos=[video],
        input_path=src_dir,
        output_path=out_dir,
        overwrite=False,
    )

    assert plan[0].action == ACTION_SKIP_CONFLICT


def test_build_watermark_plan_marks_overwrite(tmp_path: Path) -> None:
    src_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    src_dir.mkdir()
    out_dir.mkdir()
    video = src_dir / "a.mp4"
    video.write_bytes(b"")
    (out_dir / "a.mp4").write_bytes(b"existing")

    plan = build_watermark_plan(
        videos=[video],
        input_path=src_dir,
        output_path=out_dir,
        overwrite=True,
    )

    assert plan[0].action == ACTION_OVERWRITE


# ---------------------------------------------------------------------------
# format_preview
# ---------------------------------------------------------------------------


def test_format_preview_truncates_at_limit(tmp_path: Path) -> None:
    plan = [
        WatermarkPlanEntry(
            source=tmp_path / f"f{i}.mp4",
            destination=tmp_path / "out" / f"f{i}.mp4",
            action=ACTION_PROCESS,
        )
        for i in range(100)
    ]

    rendered = format_preview(plan, limit=10)

    lines = rendered.splitlines()
    # 10 visible plan lines + "...and 90 more entries" + summary tail.
    assert len(lines) == 12
    assert "...and 90 more entries" in rendered


def test_format_preview_summary_omits_zero_categories(tmp_path: Path) -> None:
    plan = [
        WatermarkPlanEntry(
            source=tmp_path / f"a{i}.mp4",
            destination=tmp_path / "out" / f"a{i}.mp4",
            action=ACTION_PROCESS,
        )
        for i in range(3)
    ]

    rendered = format_preview(plan)

    assert rendered.splitlines()[-1] == "Total: 3 to process"


# ---------------------------------------------------------------------------
# run() — argparse-driven entry point
# ---------------------------------------------------------------------------


def _base_args(**overrides: object) -> argparse.Namespace:
    """Return a fully-populated argparse.Namespace with safe defaults."""
    namespace = argparse.Namespace(
        input=None,
        output=None,
        image=None,
        text=None,
        position=DEFAULT_POSITION,
        margin=DEFAULT_MARGIN_PX,
        motion=DEFAULT_MOTION,
        motion_speed=DEFAULT_MOTION_SPEED,
        opacity=DEFAULT_OPACITY,
        scale=None,
        font_size=None,
        font_color=DEFAULT_FONT_COLOR,
        font_file=None,
        pattern="*.mp4",
        overwrite=False,
        encoder=DEFAULT_ENCODER,
        yes=True,
        quiet=False,
    )
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


def test_run_returns_setup_error_when_neither_image_nor_text_set(
    tmp_path: Path,
) -> None:
    src = tmp_path / "a.mp4"
    src.write_bytes(b"")
    args = _base_args(input=str(src), output=str(tmp_path / "out.mp4"))
    # Neither image nor text set.
    assert args.image is None and args.text is None

    rc = run(args)

    assert rc == EXIT_SETUP_ERROR


def test_run_returns_setup_error_when_both_image_and_text_set(
    tmp_path: Path,
) -> None:
    src = tmp_path / "a.mp4"
    src.write_bytes(b"")
    image = tmp_path / "wm.png"
    image.write_bytes(b"")
    args = _base_args(
        input=str(src),
        output=str(tmp_path / "out.mp4"),
        image=str(image),
        text="forbidden",
        scale=DEFAULT_IMAGE_SCALE,
        font_size=DEFAULT_FONT_SIZE,
    )

    rc = run(args)

    assert rc == EXIT_SETUP_ERROR


@pytest.mark.parametrize("bad_opacity", [-0.1, 1.5])
def test_run_returns_setup_error_on_bad_opacity(
    tmp_path: Path, bad_opacity: float
) -> None:
    src = tmp_path / "a.mp4"
    src.write_bytes(b"")
    args = _base_args(
        input=str(src),
        output=str(tmp_path / "out.mp4"),
        text="hi",
        font_size=24,
        opacity=bad_opacity,
    )

    rc = run(args)

    assert rc == EXIT_SETUP_ERROR


def test_run_returns_zero_on_no_matches(tmp_path: Path) -> None:
    empty_dir = tmp_path / "in"
    empty_dir.mkdir()
    out_dir = tmp_path / "out"
    args = _base_args(
        input=str(empty_dir),
        output=str(out_dir),
        text="hi",
        font_size=24,
        pattern="*.mp4",
    )

    rc = run(args)

    assert rc == EXIT_OK


def test_run_with_yes_flag_skips_confirm_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.mp4"
    src.write_bytes(b"")
    out = tmp_path / "out.mp4"
    args = _base_args(
        input=str(src),
        output=str(out),
        text="hi",
        font_size=24,
        yes=True,
    )

    # If questionary.confirm gets called we want a hard failure -- but the
    # module imports questionary lazily inside run(), so monkeypatch the
    # module-level binding once it has been imported. Easiest: replace the
    # subprocess call so ffmpeg "succeeds" and run() returns OK without
    # touching questionary.
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(watermark_module.subprocess, "run", fake_run)
    # _ensure_dependencies pokes shutil.which; pretend ffmpeg is on PATH.
    monkeypatch.setattr(
        watermark_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg"
    )
    # Force-clear cached binary names so the patched shutil.which is consulted.
    monkeypatch.setattr(watermark_module, "_RESOLVED_FFMPEG_BIN", None)
    monkeypatch.setattr(watermark_module, "_RESOLVED_FFPROBE_BIN", None)

    # Sentinel: if questionary.confirm somehow gets invoked, raise.
    import questionary as _q

    def boom_confirm(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("questionary.confirm should not be called when --yes is set")

    monkeypatch.setattr(_q, "confirm", boom_confirm)

    rc = run(args)

    assert rc == EXIT_OK


def test_apply_watermark_invokes_ffmpeg_with_image_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.mp4"
    src.write_bytes(b"")
    image = tmp_path / "wm.png"
    image.write_bytes(b"")
    dest = tmp_path / "out.mp4"

    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(watermark_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        watermark_module, "_RESOLVED_FFMPEG_BIN", "ffmpeg"
    )

    rc = apply_watermark_to_video(
        source=src,
        destination=dest,
        filter_arg="[1:v]format=rgba[wm];[0:v][wm]overlay=10:10",
        is_image_watermark=True,
        image_path=image,
    )

    assert rc == 0
    cmd = captured["cmd"]
    # Both -i flags appear (source THEN watermark image).
    assert cmd.count("-i") == 2
    assert str(src) in cmd
    assert str(image) in cmd
    # filter_complex is used (not -vf).
    assert "-filter_complex" in cmd
    assert "-vf" not in cmd
    # Re-encode codec settings present.
    assert "libx264" in cmd
    # Audio passes through untouched.
    assert "copy" in cmd


def test_apply_watermark_invokes_ffmpeg_with_drawtext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.mp4"
    src.write_bytes(b"")
    dest = tmp_path / "out.mp4"

    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(watermark_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        watermark_module, "_RESOLVED_FFMPEG_BIN", "ffmpeg"
    )

    rc = apply_watermark_to_video(
        source=src,
        destination=dest,
        filter_arg="drawtext=text='hi':fontsize=24:fontcolor=white@0.5:x=10:y=10",
        is_image_watermark=False,
        image_path=None,
    )

    assert rc == 0
    cmd = captured["cmd"]
    # Only one -i flag (the source video; no image input).
    assert cmd.count("-i") == 1
    # -vf used, NOT -filter_complex.
    assert "-vf" in cmd
    assert "-filter_complex" not in cmd
    # Drawtext filter argument appears verbatim.
    assert any("drawtext=" in token for token in cmd)


# ---------------------------------------------------------------------------
# has_cjk_chars / select_font_file — CJK font auto-detection
# ---------------------------------------------------------------------------


def test_has_cjk_chars_detects_chinese_han() -> None:
    # "第" / "課" are CJK Unified Ideographs (U+7B2C / U+8AB2).
    assert has_cjk_chars("第 35 課") is True


def test_has_cjk_chars_detects_hiragana() -> None:
    # ありがとう lives entirely in U+3040..U+309F.
    assert has_cjk_chars("ありがとう") is True


def test_has_cjk_chars_detects_katakana() -> None:
    # テスト lives in U+30A0..U+30FF.
    assert has_cjk_chars("テスト") is True


def test_has_cjk_chars_detects_hangul() -> None:
    # 한국어 lives in U+AC00..U+D7AF (Hangul Syllables).
    assert has_cjk_chars("한국어") is True


def test_has_cjk_chars_rejects_ascii() -> None:
    assert has_cjk_chars("Hello World") is False


def test_has_cjk_chars_rejects_punctuation_only() -> None:
    # Full-width punctuation (U+FF00 block) is intentionally NOT in our CJK
    # ranges -- DejaVu can render these glyphs fine, no font swap needed.
    assert has_cjk_chars("（）！") is False


def test_select_font_file_user_override_always_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even when text is CJK and the CJK font exists, an explicit user
    # override must be returned unchanged.
    monkeypatch.setattr(Path, "exists", lambda _self: True)
    user_path = Path("/some/custom/font.ttf")
    assert select_font_file("第 35 課", user_path) == user_path


def test_select_font_file_cjk_text_uses_cjk_font(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CJK text + CJK font present -> auto-pick the CJK font.
    monkeypatch.setattr(Path, "exists", lambda _self: True)
    chosen = select_font_file("第", user_font_file=None)
    assert chosen == Path(DEFAULT_CJK_FONT_FILE)


def test_select_font_file_cjk_text_falls_back_when_cjk_font_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CJK text but the CJK font file isn't installed -> warn + DejaVu fallback.
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    # Capture warning calls directly on the module logger so this test stays
    # robust against propagation-disabled parent loggers configured elsewhere
    # in the suite (e.g. ``configure_logging`` flips ``media_toolkit`` to
    # propagate=False, which would hide the record from caplog).
    captured: list[str] = []
    original_warning = watermark_module.logger.warning

    def capture_warning(msg: str, *args: object, **kwargs: object) -> None:
        captured.append(msg % args if args else msg)
        original_warning(msg, *args, **kwargs)

    monkeypatch.setattr(watermark_module.logger, "warning", capture_warning)

    chosen = select_font_file("第", user_font_file=None)

    assert chosen == Path(DEFAULT_FONT_FILE)
    # The warning must mention the missing CJK font path so the user can fix it.
    assert any(DEFAULT_CJK_FONT_FILE in line for line in captured)


def test_select_font_file_ascii_uses_dejavu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No CJK -> always DejaVu, even if a CJK font happens to exist.
    monkeypatch.setattr(Path, "exists", lambda _self: True)
    assert select_font_file("Hello", user_font_file=None) == Path(
        DEFAULT_FONT_FILE
    )


def test_build_drawtext_filter_picks_cjk_font_for_cjk_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end: feeding CJK text through build_drawtext_filter without a
    # font_file override should embed the CJK font path in the rendered
    # drawtext filter string.
    monkeypatch.setattr(Path, "exists", lambda _self: True)
    rendered = build_drawtext_filter(
        text="第",
        font_size=24,
        font_color="white",
        opacity=1.0,
        position=POSITION_BOTTOM_RIGHT,
        margin=20,
        motion=MOTION_STATIC,
        motion_speed=1.0,
    )
    assert DEFAULT_CJK_FONT_FILE in rendered
    # And the DejaVu default must NOT leak through alongside it.
    assert DEFAULT_FONT_FILE not in rendered


# ---------------------------------------------------------------------------
# _build_video_codec_args / encoder selection
# ---------------------------------------------------------------------------


def test_build_video_codec_args_cpu_uses_libx264() -> None:
    args = _build_video_codec_args(ENCODER_CPU)
    assert args[:2] == ["-c:v", "libx264"]
    # libx264 path uses CRF, not CQ.
    assert "-crf" in args
    assert "-cq" not in args


def test_build_video_codec_args_gpu_uses_nvenc() -> None:
    args = _build_video_codec_args(ENCODER_GPU)
    assert args[:2] == ["-c:v", "h264_nvenc"]
    # NVENC uses constant-quality (-cq), not CRF.
    assert "-cq" in args
    assert "-crf" not in args


def test_apply_watermark_uses_cpu_codec_when_encoder_cpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.mp4"
    src.write_bytes(b"")
    dest = tmp_path / "out.mp4"

    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(watermark_module.subprocess, "run", fake_run)
    monkeypatch.setattr(watermark_module, "_RESOLVED_FFMPEG_BIN", "ffmpeg")

    rc = apply_watermark_to_video(
        source=src,
        destination=dest,
        filter_arg="drawtext=text='hi':fontsize=24:fontcolor=white@1.0:x=0:y=0",
        is_image_watermark=False,
        image_path=None,
        encoder=ENCODER_CPU,
    )

    assert rc == 0
    assert "libx264" in captured["cmd"]
    assert "h264_nvenc" not in captured["cmd"]


def test_apply_watermark_uses_gpu_codec_when_encoder_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.mp4"
    src.write_bytes(b"")
    dest = tmp_path / "out.mp4"

    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(watermark_module.subprocess, "run", fake_run)
    monkeypatch.setattr(watermark_module, "_RESOLVED_FFMPEG_BIN", "ffmpeg")

    rc = apply_watermark_to_video(
        source=src,
        destination=dest,
        filter_arg="drawtext=text='hi':fontsize=24:fontcolor=white@1.0:x=0:y=0",
        is_image_watermark=False,
        image_path=None,
        encoder=ENCODER_GPU,
    )

    assert rc == 0
    assert "h264_nvenc" in captured["cmd"]
    assert "libx264" not in captured["cmd"]


def test_run_validates_encoder_choice(tmp_path: Path) -> None:
    # An invalid encoder must surface as a setup error (not crash later).
    src = tmp_path / "a.mp4"
    src.write_bytes(b"")
    args = _base_args(
        input=str(src),
        output=str(tmp_path / "out.mp4"),
        text="hi",
        font_size=24,
        encoder="quantum",
    )

    rc = run(args)

    assert rc == EXIT_SETUP_ERROR
