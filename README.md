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

Example:

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
