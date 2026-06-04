# media-toolkit

Personal Python toolkit for programmatic video, photo, and PDF editing
operations. Each operation lives under one of three top-level domains
(`videos/`, `photos/`, `pdfs/`) and is reachable via the `media-toolkit`
CLI as either an interactive menu or a direct subcommand.

## Install

Inside WSL Ubuntu (Python 3.10+):

```bash
cd /path/to/media-toolkit
pip install -e .
```

This pulls in `pysrt` and `questionary` automatically.

System dependencies (must be on `PATH`):

- `ffmpeg`
- `ffprobe` (ships with ffmpeg)

## Usage - interactive

Run with no args to get a menu:

```bash
media-toolkit
```

Flow:

1. Pick a domain (Videos / Photos / PDFs).
2. Pick an op.
3. Answer prompts (input dir, output dir, codec fallback, etc.).
4. Op runs.

Partial invocation also works — anything you supply on the command line is
kept; anything missing is prompted for. For example, `media-toolkit videos
concat --input-dir X` will prompt for the output directory and the
re-encode flag, then run.

## Logging

All progress and errors are routed through Python's `logging` module. By
default they go to **both** stdout and a log file at:

```
/tmp/media-toolkit.log
```

Override the log file location with the toplevel `--log-file` flag:

```bash
media-toolkit --log-file ~/logs/media-toolkit.log videos concat ...
```

Per-op `--quiet` suppresses console output but still writes everything to
the log file.

## Usage - subcommands

| Subcommand | Description |
| --- | --- |
| `media-toolkit videos concat --input-dir <dir> --output-dir <dir> [--reencode-on-failure] [--quiet]` | Merge sequential MP4s in a chapter folder, shift SRT timestamps, write `timestamps.txt`. |
| `media-toolkit videos watermark --input <file-or-dir> --output <file-or-dir> [--image <png>] [--text <str>] [--encoder cpu\|gpu\|auto] [--yes] [--quiet]` | Overlay an image or text watermark on a video (single file or batch). |
| `media-toolkit videos split --input <file> [--output-dir <dir>] (--parts N \| --segment-time S) [--reencode] [--yes] [--quiet]` | Split a video into N equal parts or fixed-length chunks. |

Example — concat:

```bash
media-toolkit videos concat \
    --input-dir "/path/to/source/第 1 課 ..." \
    --output-dir /path/to/combined \
    --reencode-on-failure
```

The op writes everything into `<output-dir>/<chapter-name>/`.

## Output naming convention

The merged outputs are named after the chapter (i.e. the basename of the
input directory) rather than a generic `combined.mp4`:

```
<output-dir>/
  第 1 課 為了能賞花，我們努力早起佔位置吧！/
    第 1 課 為了能賞花，我們努力早起佔位置吧！.mp4
    第 1 課 為了能賞花，我們努力早起佔位置吧！.zh-TW.srt
    第 1 課 為了能賞花，我們努力早起佔位置吧！.tw.srt
    第 1 課 為了能賞花，我們努力早起佔位置吧！.ja.srt
    第 1 課 為了能賞花，我們努力早起佔位置吧！.en-x-autogen.srt
    timestamps.txt
```

Reason: when these files inevitably get moved out of their chapter folder
(uploaded somewhere, dragged into another directory, attached to an
email, etc.), a `combined.mp4` filename loses all context. Naming the
artifact after the chapter keeps it self-describing wherever it ends up.

If a language is missing from every section in a chapter, the
corresponding merged SRT is simply not written.

## Tests

```bash
python3 -m pytest tests/ -v
```

The `videos concat` tests cover the pure-logic helpers (filename parsing,
sort key, timestamp formatting, SRT merging, concat-list construction,
section discovery, duplicate detection). The thin ffmpeg / ffprobe
wrappers are validated manually against real chapter data.

## videos split

Split a single video into multiple parts using the ffmpeg segment muxer.

### Modes

| Flag | Mode | Notes |
| --- | --- | --- |
| `--parts N` | Equal-duration parts | N parts of ~equal length. Cut points = `round(duration * k / N)` for `k = 1..N-1`. |
| `--segment-time S` | Fixed-length chunks | Chunks of S seconds; last chunk may be shorter. |

The two flags are mutually exclusive; exactly one is required.

### Method: stream copy vs re-encode

Default (no `--reencode`): **stream copy** (`-c copy`). Lossless, fast (seconds
for a multi-GB file). Cuts snap to the nearest keyframe so parts are
*approximately* (not exactly) the requested duration.

With `--reencode`: re-encodes to libx264/aac for frame-exact cut boundaries.
Slow (~10-20 min/chapter for 1080p), but parts are precisely the requested length.

If `--reencode` is not specified on the CLI, the interactive flow will
prompt a two-option menu as the last step before proceeding.

### Output naming

Parts are written to `--output-dir` (default: same directory as the input
file). Each part is named `<stem>_part<N><ext>`, numbered starting at 1:

```
input.mp4 → input_part1.mp4, input_part2.mp4, …
```

### Examples

```bash
# Split into 4 equal parts (stream copy, lossless):
media-toolkit videos split \
    --input "/path/to/lecture.mp4" \
    --parts 4 \
    --yes

# Split into 30-minute chunks (re-encode for exact boundaries):
media-toolkit videos split \
    --input "/path/to/lecture.mp4" \
    --segment-time 1800 \
    --reencode \
    --output-dir /path/to/parts \
    --yes
```

### Interactive flow

Run without `--parts` / `--segment-time` and you will be prompted for them.
If `--reencode` is not set, a two-option select appears as the final
confirmation step:

```
? Split method:
  > Stream copy (lossless, fast, keyframe-snapped)
    Re-encode (libx264/aac, exact-length parts, slow)
```

Then a plan preview is shown and a `Proceed with the split? [Y/n]` confirm
before ffmpeg is invoked. Pass `--yes` to skip the confirm in scripted use.
