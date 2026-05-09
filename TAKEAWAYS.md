# Takeaways: building `videos concat`

Lessons from Phase 1 of media-toolkit. Mostly about the ffmpeg silent-truncation bug we hit and the rabbit hole of fixing it.

## TL;DR

`ffmpeg -f concat -i list.txt -c copy` (the "concat demuxer" route, which HANDOFF.md originally specified) silently produces a truncated mp4 when input files have inconsistent PTS / timebase / fps parameters. Returns rc=0. No stderr warning. Easy to miss without a post-concat duration check.

The fallback `--reencode-on-failure` flag does NOT help — re-encode goes through the same demuxer, so the same truncation happens.

The fix: don't use the concat demuxer at all. Convert each input to MPEG-TS first (stream-copy, fast), then concat the TS files via the concat *protocol* (byte-merge), then mux back to mp4.

## The bug

### Symptom

On chapter 1 (11 sections, sum 2:56:49), `ffmpeg -f concat -i list.txt -c copy out.mp4` produced an out.mp4 of 1:58:20 — silently dropping sections 1-7 through 1-10. Plus, in the surviving 1:58:20, the slot for section 1-1 (5:39 → 24:00 in merged timeline) had frozen frames during playback.

### Root cause

ffprobe of the 11 sources revealed:

- 1-0: fps=29.97 (30000/1001), time_base=1/30000
- **1-1: fps=30/1, time_base=1/30** ← only one different
- 1-2 → 1-10: fps=29.97 (30000/1001), time_base=1/30000

The concat demuxer treats all inputs as one logical stream and assumes consistent timing. When it processes 1-1, it must convert "frame N at time_base 1/30" into "frame N at output time_base 1/30000". Since 30 and 30000/1001 are not integer-related, every frame conversion drifts ~0.0333 ms. Over 11000 frames in 1-1 plus the cumulative re-mapping of subsequent sections, by the time the demuxer reaches the 1-6 → 1-7 boundary the accumulated PTS error trips an internal sanity check, and the demuxer silently stops writing. rc=0.

### Why `--reencode-on-failure` did not help

The flag swaps `-c copy` for `-c:v libx264 -c:a aac`, but the input still flows through the same `-f concat` demuxer. The bug is in the demuxer's PTS handling, not in the codec. So re-encoded output truncates at exactly the same point, with a tiny (~21 ms) re-encode drift on the duration.

This is worth flagging as a general principle: in ffmpeg, "stream-copy fails → re-encode fixes it" is true only when the failure is in the codec layer. Demuxer / muxer / packaging issues survive a codec change because the codec sits inside the same demux → encode → mux pipeline.

## The fix: TS-intermediate + concat protocol

```
Old (broken):  11 mp4 → concat demuxer → output mp4
New (works):   11 mp4 → each → MPEG-TS → concat protocol (byte-merge) → output mp4
```

Two key choices:

### 1. MPEG-TS as intermediate container

```bash
ffmpeg -nostdin -v error -y -i 1-0.mp4 -c copy -bsf:v h264_mp4toannexb -f mpegts seg_0.ts
ffmpeg -nostdin -v error -y -i 1-1.mp4 -c copy -bsf:v h264_mp4toannexb -f mpegts seg_1.ts
...
```

TS is designed for broadcast streaming. Every TS packet carries its own self-contained PTS / DTS, with no global timeline metadata (mp4's `mvhd` / `elst` etc.) to coordinate. Converting mp4 → TS forces ffmpeg to re-sequence PTS for each input independently, which absorbs 1-1's anomalous time_base.

`-bsf:v h264_mp4toannexb` is a bitstream filter that converts H.264 NAL units from mp4's `avc1` format (length-prefixed) to TS's `Annex-B` format (start-code-prefixed). This is a packaging change only — no decode, no re-encode.

### 2. Concat protocol instead of concat demuxer

```bash
ffmpeg -nostdin -v error -y -i "concat:seg_0.ts|seg_1.ts|...|seg_10.ts" \
    -c copy -bsf:a aac_adtstoasc out.mp4
```

`concat:a|b|c` is an ffmpeg *protocol* (same level as `file:`, `http:`), not a demuxer. It does the simplest possible thing: byte-concatenate a, b, c into a single virtual input. PTS accumulates naturally from 0. This works for TS containers because TS packets are self-aligned 188-byte structures; it does not work for mp4 because mp4 needs structural integrity (moov atom etc.).

`-bsf:a aac_adtstoasc` converts AAC audio from TS's ADTS format back to mp4's ASC format for the final mux.

## Speed comparison

| Method | Wall time | Notes |
|---|---|---|
| concat demuxer + `-c copy` (HANDOFF default) | ~12 s | Pure byte transfer, but **truncated output** |
| concat demuxer + re-encode (HANDOFF fallback) | ~16 min | libx264 re-encodes every frame, **still truncated** |
| **TS intermediate + concat protocol (new default)** | **~66 s** | Both stages stream-copy; ~50 s spent on per-source TS rewrap (~4 s each) |

The new path is the same order of magnitude as the original stream-copy (not the re-encode order of magnitude), because nothing decodes or re-encodes — we just unwrap mp4 packaging, rewrap as TS, then unwrap TS and rewrap as mp4 again.

## Other things worth remembering

### Snap ffmpeg quirks (Ubuntu 22.04)

The user's WSL has snap-installed ffmpeg. Two surprises:

1. **Binary names.** `ffmpeg` works, but `ffprobe` is exposed as `ffmpeg.ffprobe` (not `ffprobe`). Our defensive resolver tries `ffprobe` first, then falls back to `ffmpeg.ffprobe`.
2. **Sandbox restricts `/tmp`.** Snap-confined ffmpeg cannot read or write files in the host `/tmp`. Putting the concat-list file or TS workdir in `output_path.parent` (which is on `/mnt/d/...`, outside the snap sandbox) avoids this.

### Always pass `-nostdin` to ffmpeg

When ffmpeg runs inside a heredoc, pipe, or any context where it shares stdin with a parent script, it will read commands from stdin (interactive console mode) and silently consume the rest of your script as ffmpeg commands. Symptom: bash reports "syntax error near `done`" after ffmpeg ran. Always pass `-nostdin`.

### Always verify the output, not just the exit code

`subprocess.run(...).returncode == 0` is necessary but not sufficient. For ffmpeg specifically (and any tool that handles streams of data), add a post-condition: did the output match what we expected? In our case, comparing merged duration to the sum of source durations (with ±2s tolerance) is a one-line check that catches every silent truncation we have seen.

### Subagent-spawned background processes die when the subagent ends

We tried running the long re-encode in a subagent's `Bash run_in_background`. When the subagent exited, its bash session was reaped and the ffmpeg child died with it (output dir had a 2 MB partial mp4). Long-running background commands have to be kicked off from the main session, where the bash shell persists for the conversation.
