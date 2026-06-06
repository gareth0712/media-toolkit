# media-toolkit

Personal Python toolkit for programmatic video, photo, and PDF editing
operations. Each operation lives under one of three top-level domains
(`videos/`, `photos/`, `pdfs/`) and is reachable via the `media-toolkit`
CLI as either an interactive menu or a direct subcommand.

---

## Commands at a glance

| Op | Purpose | Most common invocation |
|----|---------|------------------------|
| `videos concat` | Merge sequential MP4s in a chapter folder, shift SRT timestamps, write `timestamps.txt` | `media-toolkit videos concat --input-dir /path/to/chapter --output-dir /path/to/out` |
| `videos watermark` | Overlay an image or text watermark (single file or batch directory) | `media-toolkit videos watermark --input /path/to/video.mp4 --output /path/to/out.mp4 --text "My Brand" --encoder auto` |
| `videos split` | Split a video into N equal parts or fixed-length chunks | `media-toolkit videos split --input /path/to/video.mp4 --parts 4` |

---

## Usage — interactive

**This is the primary way to use media-toolkit.** No flags to memorize — just run:

```bash
media-toolkit
```

The menu walks you through everything.

### Step 1 — pick a domain

```
? What type of media?
  > Videos
    Photos
    PDFs
    Files
    Quit
```

Select **Videos** and press Enter.

### Step 2 — pick an operation

```
? Choose an operation:
  > concat - Merge sequential MP4s in a chapter folder, shift SRT timestamps, and write a YouTube-style timestamps.txt
    watermark - Overlay an image or text watermark on a video (single file or batch).
    split - Split a video into N equal parts or fixed-length chunks (stream copy or re-encode).
    Quit
```

---

### Walkthrough: `videos concat`

**What it does:** Scans a chapter directory for numbered section MP4s (e.g. `1-1 intro.mp4`, `1-2 body.mp4`), merges them in order into a single MP4, merges their `.srt` subtitle files with shifted timestamps, and writes a `timestamps.txt` in YouTube chapter format.

Select **concat** from the op menu, then answer these prompts:

```
? Input directory: /path/to/第 1 課 為了能賞花
? Output directory: /path/to/combined
? Re-encode if stream-copy fails? (Y/n) Y
```

Output written to `<output-dir>/<chapter-name>/`:

```
/path/to/combined/
  第 1 課 為了能賞花/
    第 1 課 為了能賞花.mp4
    第 1 課 為了能賞花.zh-TW.srt
    第 1 課 為了能賞花.tw.srt
    第 1 課 為了能賞花.ja.srt
    第 1 課 為了能賞花.en-x-autogen.srt
    timestamps.txt
```

> **Note on naming:** Output files are named after the chapter (basename of `--input-dir`), not a generic `combined.mp4`, so they stay self-describing when moved.

---

### Walkthrough: `videos watermark`

**What it does:** Applies an image (PNG with alpha) or text watermark to a single video or a whole directory of videos. Always re-encodes (no stream copy). Shows a plan preview before running.

Select **watermark** from the op menu, then answer these prompts:

**Image watermark example:**

```
? Input video or directory: /path/to/lecture.mp4
? Output file or directory: /path/to/lecture_wm.mp4
? Watermark type:
  > Image
    Text
? Watermark image (PNG): /path/to/logo.png
? Motion:
  > static (fixed position)
    bounce (diagonal)
    drift (slow wander)
? Position:
  > bottom-right
    bottom-left
    top-left
    top-right
    center
? Opacity (0.0-1.0): 0.5
? Image scale (fraction of video width): 0.15
? Encoder:
  > auto (use GPU when available, fall back to CPU)
    cpu (libx264, portable)
    gpu (h264_nvenc, force GPU - fails if unavailable)
? Overwrite existing output files? (y/N) N
```

A plan preview is then shown:

```
process    lecture.mp4 -> /path/to/lecture_wm.mp4
Total: 1 to process
? Proceed with 1 entries? (y/N)
```

**Text watermark example (batch directory):**

```
? Input video or directory: /path/to/videos/
? Output file or directory: /path/to/watermarked/
? Watermark type:
    Image
  > Text
? Watermark text: My Channel
? Motion:
  > static (fixed position)
    ...
? Position: bottom-right
? Opacity (0.0-1.0): 0.5
? Font size (px): 36
? Encoder: auto (use GPU when available, fall back to CPU)
? Overwrite existing output files? (y/N) N
```

> **Batch tip:** By default the directory scan uses glob `*.mp4`. Pass `--pattern "**/*.mp4"` on the command line to recurse into subdirectories.

---

### Walkthrough: `videos split`

**What it does:** Splits a single video into multiple parts. Two modes: equal parts or fixed-length chunks. Default is lossless stream copy (fast, cuts snap to keyframes); optional re-encode for frame-exact cuts. Shows a plan preview before running.

Select **split** from the op menu, then answer these prompts:

**Equal parts example (stream copy):**

```
? Input video file: /path/to/lecture.mp4
? Split into:
  > Equal parts (N pieces of equal length)
    Fixed-length chunks (every N seconds)
? Number of parts: 4
? Split method:
  > Stream copy (lossless, fast, keyframe-snapped)
    Re-encode (libx264/aac, exact-length parts, slow)
```

The plan preview is shown and a confirm prompt appears:

```
Split plan:
  input:         lecture.mp4
  mode:          equal 4 parts
  cut points:    1941.673,3883.346,5825.019
  expected parts: 4
  method:        stream copy (lossless)
  output pattern: /path/to/lecture_part%d.mp4
? Proceed with the split? (Y/n)
```

**Fixed-length chunks example:**

```
? Input video file: /path/to/lecture.mp4
? Split into:
    Equal parts (N pieces of equal length)
  > Fixed-length chunks (every N seconds)
? Chunk length in seconds: 600
? Split method:
  > Stream copy (lossless, fast, keyframe-snapped)
    Re-encode (libx264/aac, exact-length parts, slow)
```

Output files are named `<stem>_part1.mp4`, `<stem>_part2.mp4`, … in the same directory as the input (or `--output-dir` if specified).

> **Re-encode mode:** Choose *Re-encode (libx264/aac, exact-length parts, slow)* when you need frame-exact cut boundaries. Expect ~10-20 min/chapter for 1080p.

---

## Usage — subcommands

For scripted/automated use, pass all flags directly. Anything not supplied falls back to interactive prompts.

### `videos concat`

```bash
media-toolkit videos concat \
    --input-dir "/path/to/chapter" \
    --output-dir /path/to/out \
    [--reencode-on-failure] \
    [--quiet]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--input-dir` | path | _(prompted)_ | Chapter directory with section MP4s and SRTs |
| `--output-dir` | path | _(prompted)_ | Root output directory; a subfolder named after the chapter is created |
| `--reencode-on-failure` | flag | _(prompted)_ | Fall back to libx264/aac if stream-copy fails |
| `--quiet` | flag | off | Suppress console output (log file unaffected) |

### `videos watermark`

```bash
media-toolkit videos watermark \
    --input <file-or-dir> \
    --output <file-or-dir> \
    (--image <png> | --text <str>) \
    [--position top-left|top-right|bottom-left|bottom-right|center] \
    [--motion static|bounce|drift] \
    [--motion-speed FLOAT] \
    [--opacity FLOAT] \
    [--scale FLOAT] \
    [--font-size INT] \
    [--font-color STR] \
    [--font-file PATH] \
    [--pattern GLOB] \
    [--encoder auto|cpu|gpu] \
    [--overwrite] \
    [--yes] \
    [--quiet]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--input` | path | _(prompted)_ | Single video file **or** directory of videos |
| `--output` | path | _(prompted)_ | Output file (single) or directory (batch) |
| `--image` | path | — | PNG watermark image (mutually exclusive with `--text`) |
| `--text` | string | — | Text watermark string (mutually exclusive with `--image`) |
| `--position` | choice | `bottom-right` | Static position preset; ignored when `--motion` is not `static` |
| `--margin` | int | `20` | Pixels from edge for static position presets |
| `--motion` | choice | `static` | Motion mode: `static`, `bounce` (diagonal Pong), `drift` (slow wander) |
| `--motion-speed` | float | `1.0` | Motion speed multiplier |
| `--opacity` | float | `0.5` | Watermark opacity 0.0–1.0 |
| `--scale` | float | `0.15` | Image width as fraction of video width (image mode only) |
| `--font-size` | int | `36` | Font size in pixels (text mode only) |
| `--font-color` | string | `white` | Font colour name or `#RRGGBB` (text mode only) |
| `--font-file` | path | DejaVuSans | TTF/OTF font file; bypasses fontconfig (required for snap ffmpeg) |
| `--pattern` | glob | `*.mp4` | Glob pattern when `--input` is a directory; use `**/*.mp4` to recurse |
| `--encoder` | choice | `auto` | `auto` = GPU if available else CPU; `cpu` = libx264; `gpu` = h264_nvenc |
| `--overwrite` | flag | off | Overwrite existing output files (default: skip conflicts) |
| `--yes` | flag | off | Skip the confirm prompt |
| `--quiet` | flag | off | Suppress per-file progress output |

> Watermark always re-encodes — no stream-copy option.

### `videos split`

```bash
media-toolkit videos split \
    --input <file> \
    (--parts N | --segment-time SECONDS) \
    [--output-dir <dir>] \
    [--reencode] \
    [--yes] \
    [--quiet]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--input` | path | _(prompted)_ | Source video file |
| `--output-dir` | path | input's directory | Directory for output parts (created if absent) |
| `--parts N` | int | — | Split into N equal-duration parts (mutually exclusive with `--segment-time`) |
| `--segment-time S` | float | — | Split into fixed-length chunks of S seconds; last chunk may be shorter (mutually exclusive with `--parts`) |
| `--reencode` | flag | off (stream copy) | Re-encode with libx264/aac for frame-exact cuts (slow); default is lossless stream copy |
| `--yes` | flag | off | Skip the confirm prompt |
| `--quiet` | flag | off | Suppress console output |

**Output naming:** `<stem>_part1.<ext>`, `<stem>_part2.<ext>`, … in `--output-dir`.

```bash
# 4 equal parts, stream copy (lossless):
media-toolkit videos split --input lecture.mp4 --parts 4 --yes

# 30-minute chunks, frame-exact:
media-toolkit videos split --input lecture.mp4 --segment-time 1800 --reencode --output-dir /path/to/parts --yes
```

---

## Install

### 1. Install pipx

Install pipx for your OS: <https://pipx.pypa.io/stable/installation/>

| OS | Command |
|----|---------|
| macOS | `brew install pipx && pipx ensurepath` |
| Windows | `scoop install pipx` or `winget install pipx` |
| Debian/Ubuntu | `sudo apt install pipx && pipx ensurepath` |

### 2. Install media-toolkit

```bash
pipx install "git+https://github.com/gareth0712/media-toolkit.git@v0.2.0"
```

Upgrade later:

```bash
pipx upgrade media-toolkit
# or reinstall a specific version:
pipx install "git+https://github.com/gareth0712/media-toolkit.git@vX.Y.Z" --force
```

Or from a downloaded wheel:

```bash
pipx install ./media_toolkit-0.2.0-py3-none-any.whl
```

### 3. Install ffmpeg

`ffprobe` ships with ffmpeg. Both must be on `PATH`.

| OS | Command |
|----|---------|
| macOS | `brew install ffmpeg` |
| Windows | `winget install ffmpeg` (or [Gyan build](https://www.gyan.dev/ffmpeg/builds/)) |
| Debian/Ubuntu | `sudo apt install ffmpeg` |

### Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` on `PATH`

---

## Releasing

1. Bump `[project] version` in `pyproject.toml` (semver, e.g. `0.2.0 → 0.3.0`).
2. Tag and push:
   ```bash
   git tag v0.3.0 && git push origin v0.3.0
   ```
3. GitHub Actions (`release.yml`) triggers on the tag: builds the universal wheel + sdist via `python -m build` on Ubuntu, then publishes a GitHub Release with `dist/*.whl` and `dist/*.tar.gz` attached.
4. Users install/upgrade with pipx — no repo clone needed:
   ```bash
   pipx install "git+https://github.com/gareth0712/media-toolkit.git@v0.3.0"
   # or
   pipx upgrade media-toolkit
   ```
5. _(optional)_ Verify a downloaded wheel's signed build provenance (the release
   job runs with least privilege; the build job emits an OIDC-signed attestation):
   ```bash
   gh attestation verify media_toolkit-0.3.0-py3-none-any.whl --repo gareth0712/media-toolkit
   ```

> **Note:** The GitHub Actions workflow cannot be fully validated without pushing a real tag (which cuts a real Release). Local `python -m build` + YAML parse is the pre-merge gate; the first real tag validates the workflow end-to-end. Do NOT push a tag during development.

---

## Logging

All progress and errors route through Python's `logging` module — both stdout and a log file:

```
/tmp/media-toolkit.log
```

Override the log path:

```bash
media-toolkit --log-file ~/logs/media-toolkit.log videos concat ...
```

Per-op `--quiet` suppresses console output but still writes to the log file.

---

## Output naming convention (concat)

Merged outputs are named after the chapter (basename of `--input-dir`):

```
<output-dir>/
  第 1 課 為了能賞花/
    第 1 課 為了能賞花.mp4
    第 1 課 為了能賞花.zh-TW.srt
    第 1 課 為了能賞花.ja.srt
    第 1 課 為了能賞花.en-x-autogen.srt
    timestamps.txt
```

If a language is missing from every section, the merged SRT for that language is not written.

---

## Tests

```bash
python3 -m pytest tests/ -v
```

The `videos concat` tests cover pure-logic helpers (filename parsing, sort key, timestamp formatting, SRT merging, concat-list construction, section discovery, duplicate detection). The thin ffmpeg/ffprobe wrappers are validated manually against real chapter data.
