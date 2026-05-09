# media-toolkit — Handoff & Development Brief

## 0. TL;DR for a fresh agent

You are joining a brand-new repo for a personal media-processing toolkit.
Read this whole file before writing any code. The repo currently only has a
placeholder layout — your first job is to set up the proper package
structure and implement the first concrete operation (Phase 1 below).

---

## 1. Vision

Personal Python toolkit for programmatic media editing operations. Each
operation is a callable module under one of three top-level domains
(`videos/`, `photos/`, `pdfs/`). Operations are accessible two ways:

1. **Interactive CLI** — run `media-toolkit` with no args, get a menu:
   pick media type → pick operation → answer prompts → run.
2. **Direct CLI subcommands** — `media-toolkit videos concat --input-dir
   ... --output ...` for scripting / batch use.

Both routes call the same underlying functions; the menu is just a thin
wrapper that gathers args interactively.

### Non-goals

- Not a video editor (no UI, no preview).
- Not a SaaS or library for distribution. Personal use; install locally.
- Not a wrapper around a single tool — ffmpeg, Pillow, pypdf etc. are all
  fair game depending on op.

---

## 2. Recommended repo structure

```
media-toolkit/                       (this directory — hyphenated git name OK)
  pyproject.toml                     (entry point + deps; you create this)
  README.md
  HANDOFF.md                         (this file)
  LICENSE
  .gitignore                         (already populated, Python boilerplate)
  media_toolkit/                     (importable package — underscores)
    __init__.py
    __main__.py                      (`python -m media_toolkit` entry)
    cli.py                           (interactive menu + subcommand dispatch)
    videos/
      __init__.py
      concat.py                      (Phase 1 op)
      srt_merge.py                   (Phase 1 op)
      timestamps.py                  (Phase 1 op)
    photos/
      __init__.py                    (empty for now; future ops)
    pdfs/
      __init__.py                    (empty for now; future ops)
  tests/
    test_videos_concat.py
    test_videos_srt_merge.py
    fixtures/
      tiny_a.mp4
      tiny_b.mp4
      tiny_a.zh-TW.srt
      tiny_b.zh-TW.srt
```

### What to delete from the current state

- `media-toolkit/__init__.py` (at repo root) — useless, hyphenated dir
  cannot be imported as a Python package. The `media_toolkit/__init__.py`
  inside the new package dir is the right place for `__init__.py`.
- `media-toolkit/{videos,photos,pdfs}/` (at repo root) — these are empty
  dirs in the wrong location. Recreate them inside `media_toolkit/`.

---

## 3. CLI design

### Interactive mode (no args)

```
$ media-toolkit
? What type of media? (Use arrow keys)
> Videos
  Photos
  PDFs
  Quit

? Choose a video operation:
> Concat (merge sequential MP4s with SRT timestamp shift + chapter list)
  Watermark (NOT YET IMPLEMENTED)
  Quit

? Input directory: /mnt/d/Downloads/wordup/source
? Output directory: /mnt/d/Downloads/wordup/combined
? Re-encode if stream-copy fails? (Y/n)
...
```

Use **`questionary`** for the prompts. Concise menu, arrow-key nav, sensible
defaults remembered for the session if cheap.

### Subcommand mode (scripted)

```
media-toolkit videos concat \
    --input-dir /mnt/d/Downloads/wordup/source \
    --output-dir /mnt/d/Downloads/wordup/combined \
    --reencode-on-failure
```

Use **argparse** for subcommands (no extra dep). Each op module exports a
`register_subparser(subparsers)` and a `run(args)` so `cli.py` can wire them
in uniformly.

### Pattern for every op module

```python
# media_toolkit/videos/concat.py

NAME = "concat"
DESCRIPTION = "Merge sequential MP4s in a folder, shift SRT timestamps, write timestamps.txt"

def register_subparser(subparsers):
    p = subparsers.add_parser(NAME, help=DESCRIPTION)
    p.add_argument("--input-dir", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--reencode-on-failure", action="store_true")
    p.set_defaults(func=run)

def interactive_args():
    """Called by interactive menu; returns argparse.Namespace-like object."""
    import questionary
    return SimpleNamespace(
        input_dir=questionary.path("Input directory:").ask(),
        output_dir=questionary.path("Output directory:").ask(),
        reencode_on_failure=questionary.confirm("Re-encode if stream-copy fails?", default=True).ask(),
    )

def run(args):
    """Do the work. Return 0 on success, non-zero on failure."""
    ...
```

`cli.py` discovers ops by importing each subpackage's `__init__.py`, which
re-exports a list of op modules. Adding a new op = create the file +
register it in the parent `__init__.py`. No central registry to maintain
beyond that.

---

## 4. Dependencies

### Python (pyproject.toml)

- `pysrt` — SRT cue parsing + time shifting
- `questionary` — interactive prompts

### System (assumed installed in WSL)

- `ffmpeg` (snap on user's machine, on PATH)
- `ffprobe` (comes with ffmpeg)

Don't add `ffmpeg-python` — `subprocess.run` is enough and avoids dragging
in a heavy dep tree for what we need.

### Future (don't add until needed)

- `Pillow` — for `photos/`
- `pypdf` or `pdfplumber` — for `pdfs/`

---

## 5. Phase 1 — first concrete op: `videos concat`

This is the immediate motivating use case. The user just downloaded a 35-
chapter Japanese course (`material 744` from wordup.com.tw) using a
sibling repo (`wordup-scraper`). All 267 video files + multilingual SRTs
are sitting on disk waiting to be merged into one file per chapter.

### Source data layout (input)

```
/mnt/d/Downloads/wordup/source/
  第 1 課 為了能賞花，我們努力早起佔位置吧！/
    1-0 關於這個課程__解惑.mp4
    1-0 關於這個課程__解惑.zh-TW.srt
    1-0 關於這個課程__解惑.ja.srt
    1-0 關於這個課程__解惑.tw.srt
    1-0 關於這個課程__解惑.en-x-autogen.srt
    1-1 重點＋對話__解惑.mp4
    1-1 重點＋對話__解惑.zh-TW.srt
    ...
    1-10 對話解析__解惑.mp4         # NB: 1-10 must sort AFTER 1-9
    ...
  第 2 課 .../
    ...
  第 35 課 .../
```

- Filename format: `<section>__<component>.{mp4|<lang>.srt}`
- Section like `1-2 慣用句：同意 いいですね ／ いいですよ`. Forward slashes
  inside section names are full-width `／` (U+FF0F) — keep them as-is in
  output filenames.
- Component is always `解惑` for this material. Don't hardcode that.
- Subtitle languages observed: `zh-TW`, `tw`, `ja`, `en-x-autogen`. Some
  sections may be missing one of them — handle gracefully.

### Target output layout

```
/mnt/d/Downloads/wordup/combined/
  第 1 課 為了能賞花，我們努力早起佔位置吧！/
    第 1 課 為了能賞花，我們努力早起佔位置吧！.mp4
    第 1 課 為了能賞花，我們努力早起佔位置吧！.zh-TW.srt
    第 1 課 為了能賞花，我們努力早起佔位置吧！.tw.srt
    第 1 課 為了能賞花，我們努力早起佔位置吧！.ja.srt
    第 1 課 為了能賞花，我們努力早起佔位置吧！.en-x-autogen.srt
    timestamps.txt
  第 2 課 .../
  ...
```

Or if the chapter-name-as-filename feels redundant, name them `combined.mp4`
inside each chapter folder. Pick one and document the choice in the README.

### Per chapter, the op produces

1. **Merged MP4** — concat all sections in natural numeric order (`1-0,
   1-1, 1-2, ..., 1-10`). NOT lexicographic — `1-10` must come after `1-9`.
2. **Merged SRT per language** (zh-TW, tw, ja, en-x-autogen) — concat in
   the same order with each cue's start/end timestamps shifted by the
   cumulative duration of all preceding videos.
3. **`timestamps.txt`** — YouTube chapter format, one line per section:
   ```
   00:00:00 1-0 關於這個課程__解惑
   00:05:56 1-1 重點＋對話__解惑
   00:11:43 1-2 慣用句：同意 いいですね ／ いいですよ__解惑
   ...
   ```

### Implementation guidance

#### Natural sort

```python
import re
def section_sort_key(filename):
    m = re.match(r"(\d+)-(\d+)", filename)
    return (int(m.group(1)), int(m.group(2)))
```

#### MP4 concat — try stream-copy first

All videos came from Vimeo's HLS pipeline so codecs almost certainly match.

```bash
# inside a chapter dir
ls *.mp4 | python3 -c "<your sort + escape>" > /tmp/list.txt
ffmpeg -f concat -safe 0 -i /tmp/list.txt -c copy combined.mp4
```

Build the filelist programmatically in Python (don't shell-pipe) — section
names contain spaces, slashes, full-width chars. Use ffmpeg concat demuxer
format:
```
file '/abs/path/with spaces/section.mp4'
```
Single quotes, escape any embedded single quotes by doubling them. Use
absolute paths so `-safe 0` isn't needed (or just keep `-safe 0`).

If `-c copy` fails or produces glitchy output (rare; SAR/fps/codec
mismatch), fall back to re-encode:
```bash
ffmpeg -f concat -safe 0 -i /tmp/list.txt -c:v libx264 -preset medium -crf 20 -c:a aac -b:a 128k combined.mp4
```

Re-encode is 5-10x slower (10-20 min/chapter vs 1-2 min). Stream-copy
should work for 95%+ of cases here. Make `--reencode-on-failure` opt-in
via the CLI flag.

#### SRT concat with timestamp shift (the tricky part)

The user explicitly raised this. Here's the canonical approach:

```python
import pysrt
import subprocess

def get_duration_ms(mp4_path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(mp4_path),
    ])
    return int(float(out.strip()) * 1000)

def merge_srts(srt_paths, mp4_paths, output_path):
    """
    srt_paths and mp4_paths are parallel lists in section order.
    Each srt_paths[i] may be None if that language is missing for that section
    — the offset still advances based on mp4_paths[i] duration.
    """
    merged = pysrt.SubRipFile()
    offset_ms = 0
    for srt_path, mp4_path in zip(srt_paths, mp4_paths):
        if srt_path is not None:
            subs = pysrt.open(srt_path)
            subs.shift(milliseconds=offset_ms)
            merged.extend(subs)
        offset_ms += get_duration_ms(mp4_path)
    for i, sub in enumerate(merged, 1):
        sub.index = i
    merged.save(str(output_path), encoding="utf-8")
```

**Why ffprobe and not the SRT's last-cue end time:** Vimeo's SRTs sometimes
end a second or two before the actual video ends. Using the SRT to compute
offset will drift across 11 sections.

**Worked example matching what the user described:**

- Video 1-1 is 5:00 long. SRT 1-1 cue at `00:00:03 hello world!` stays at
  `00:00:03` (offset = 0).
- After processing 1-1: offset = 300_000 ms.
- Video 1-2's SRT first cue is `00:00:03 hello world!`. After
  `subs.shift(milliseconds=300_000)`: `00:05:03 hello world!`. Correct.
- After processing 1-2: offset = 480_000 ms (300k + 180k).
- And so on for 1-3, 1-4, ...

**Cosmetic edge case:** if SRT 1-1's last cue ends slightly past 5:00 (say
`00:05:01,500 --> 00:05:02,200`), and SRT 1-2's first cue after shift is
`00:05:03`, no overlap → fine. If SRT 1-1's last cue ends at `00:05:03,500`
AND SRT 1-2 first cue after shift is `00:05:03`, they overlap by 500ms.
Visible to no one in normal playback, but to be strict you can clamp each
SRT's last cue end to `min(end, mp4_duration_ms)` before merging. Optional.

#### timestamps.txt

```python
def format_timestamp(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

# Iterate sections in order, write line per section using cumulative offset
```

Use `HH:MM:SS` (zero-padded hours). YouTube also accepts `H:MM:SS` for
under-1h videos but `HH` is unambiguous. First entry is always `00:00:00`.

### Validation checklist (run on chapter 1 first, ALWAYS)

1. Produce all outputs for chapter 1.
2. Open the merged MP4 — verify smooth playback across section boundaries
   (listen for audio glitches, watch for video stutters).
3. Open zh-TW SRT in mpv/VLC alongside the MP4. Check sync at:
   - Start of section 1-0
   - Middle of a long section (e.g. 1-3)
   - Section boundary (last cue of 1-2 vs first cue of 1-3)
   - Final section
4. Open `timestamps.txt`. Click each timestamp in YouTube/VLC, confirm it
   lands on the start of the named section.
5. Only after all 4 pass for chapter 1, run on remaining 34 chapters.

### Things to watch for in the wordup data specifically

- **Chapter 1 may have stale duplicates** with double-spaces where slashes
  used to be (older filename scheme). User confirmed they re-downloaded;
  the dir SHOULD be clean. If your code sees both `いいですね  いいですよ`
  (double space) and `いいですね ／ いいですよ` (with full-width slash),
  STOP and confirm with the user — don't process duplicates blindly.
- **Section number gaps are normal.** Some chapters skip section indices
  because non-video sections (cards, exercises) weren't downloaded. Don't
  warn or error on `1-0, 1-1, 1-3, 1-4` (missing 1-2) — just process what
  exists.
- **Component is always `解惑`** for this material but don't hardcode that
  string. Future materials may differ.

---

## 6. Future operations to plan for (not now, but design accommodates)

### `videos/`
- `watermark` — overlay PNG/text on video
- `audio_extract` — pull audio track to MP3/WAV
- `transcode` — re-encode for size / format
- `clip` — extract time range
- `thumbnail` — single frame extraction

### `photos/`
- `watermark`
- `resize` (batch)
- `convert` (format)
- `exif_strip`
- `collage`

### `pdfs/`
- `merge`
- `split`
- `extract_pages`
- `watermark`
- `ocr`

Some operations span domains (`watermark` for both videos and photos). For
now, implement them under whichever domain they're first needed for —
refactor to a shared module only when actual code duplication emerges.

---

## 7. Conventions

- **Absolute paths.** Every op accepts and returns absolute paths. No "cwd
  magic."
- **Non-destructive by default.** Write to new file. Don't overwrite source
  unless `--in-place` is given (no op currently has this).
- **`--verbose` / `--quiet`.** Default to a reasonable middle.
- **Progress.** Long-running ops print `[1/35] processing ...` so the user
  knows it's alive.
- **Exit codes.** 0 = all good, 1 = at least one item failed (continue
  others), 2 = setup error (bad args, missing tool).
- **Tests.** At minimum, smoke-test each op with a tiny fixture (1-2s MP4,
  6-cue SRT). `pytest`. Test fixtures in `tests/fixtures/`.

---

## 8. Phase 1 acceptance criteria

You're done with Phase 1 when:

- [ ] Repo restructured per Section 2 (root `__init__.py` + empty subdirs
      removed; `media_toolkit/` package created)
- [ ] `pyproject.toml` defines `media-toolkit` entry point pointing at
      `media_toolkit.cli:main`
- [ ] `pip install -e .` works in WSL; `media-toolkit --help` lists `videos
      concat` subcommand; bare `media-toolkit` opens interactive menu
- [ ] `videos/concat.py` implements the spec in Section 5
- [ ] Tests pass (at minimum: tiny-fixture smoke test for concat + SRT
      shift)
- [ ] Chapter 1 of `/mnt/d/Downloads/wordup/source/` produces clean output;
      validation checklist all green
- [ ] README.md written: install instructions, interactive flow demo,
      subcommand reference table
- [ ] User confirms output before you run on remaining 34 chapters

Don't run on all 35 chapters until the user reviews chapter 1. That's a
~1-2 hour batch and rerunning after a discovered bug means re-doing all of
it (the op should be idempotent, but still — no point burning the time).

---

## 9. Tooling on the user's machine (verified)

- WSL Ubuntu 22.04
- `python3` 3.10
- `pip3` 22.0.2
- `ffmpeg` (snap, on PATH)
- `ffprobe` (with ffmpeg)
- All ops should be invokable from Windows-side bash via `wsl -- bash -lc
  '...'` since the user runs WSL for everything

---

## 10. Reference: sibling repo

Companion downloader: `S:\git\3-useful-tools\wordup-scraper\` — handles
fetching the source data. **Do not modify it.** It's already shipped to
GitHub. Read its README if you want to understand how the source files
were named the way they are, but treat it as a black-box producer.
