# Workflow

Development patterns that worked well in this project. Notes for future me and collaborators.

## 1. Implement-verify loop until totally working

For each feature: implement -> run real-data tests -> fix discovered issues -> re-test -> repeat until programmatic verification passes. Don't declare "done" based on unit tests alone -- unit tests verify code correctness, real-data smoke tests verify feature correctness.

Example: `videos/concat` went through 4 iterations:
1. First impl with concat-demuxer -- passed unit tests
2. Smoke on real chapter 1 -- MP4 silently truncated at 1:58:20
3. Added duration-mismatch detection -- re-encode fallback also truncated
4. Diagnosed root cause (PTS bug in concat demuxer with mixed time_base sources)
5. Switched to TS-intermediate route -- all 35 chapters passed

The loop was not "done at iteration 1" just because tests were green.

## 2. Most-feasible approach first; commit before exploring alternatives

When multiple approaches exist with different feasibility:
1. Pick the one most likely to work
2. Implement, verify, **commit** (locks in known-good state)
3. Then explore the riskier/more-novel alternative
4. If alternative fails or is worse: rollback is trivial (the prior commit is the safe state)
5. If alternative wins: commit as the next iteration

Example: GPU encoder. Tried preset `fast` first (A) -- worked, committed (29b3a8a). Then explored `overlay_cuda` full-GPU pipeline (C) -- discovered limitations in apt ffmpeg 4.4.2, rejected. The A commit was already safe; no rollback drama.

## 3. Investigate before implementation when surprised

When something fails unexpectedly, diagnose **root cause** before patching.

Examples:
- concat truncated at 1:58:20: ffprobe each of 11 source mp4s -> found 1-1 had different fps/timebase -> root cause was concat demuxer's PTS handling, not codec
- watermark filter `No such filter: '2*(W-tw))-(W-tw)):y'`: traced to comma in `mod(80*t, 2*W)` being interpreted as filter chain separator -> fixed with backslash escape
- Snap ffmpeg `Cannot load libcuda.so.1`: traced to snap sandbox lacking access to host CUDA libs -> switched to apt ffmpeg

This sometimes uncovers things you'd never have considered (the snap fontconfig mess; the snap `/tmp` sandbox; etc.). All would have stayed mysterious if we'd patched symptoms blindly.

## 4. Add post-condition checks for silent-failure-prone tools

ffmpeg often returns rc=0 even when output is wrong (silent truncation, codec mismatch, duration drift). For these, returncode is necessary but not sufficient. Add a post-condition:

```python
if abs(actual_duration_ms - expected_duration_ms) > tolerance_ms:
    raise StreamCopyFailedError(...)
```

Same principle for any tool that processes streams of data and reports rc=0 -- it might be silently mangling output. Validate the output at the boundary of trust transfer.

## 5. Real-data smoke tests on representative samples

Unit tests with mocked ffmpeg verify our wiring. Real-data smoke tests verify the feature actually works. Both are needed.

Sample file budget: keep small fixtures for fast iteration (30s trim) plus one full-length sample for end-to-end (1.2hr). Don't always use full samples -- slow iteration kills momentum.

Real samples also surface edge cases unit tests miss (Windows paths through questionary, snap sandbox quirks, CJK character handling, NVENC preset quirks, etc.).

## 6. Background long jobs from main session, not subagent

Subagent dies -> its background processes die with it. For long-running jobs (re-encode, batch processing):
- Kick off `Bash run_in_background=true` from the **main orchestrator session**, not a subagent
- Set up `ScheduleWakeup` as a fallback heartbeat (1200-1800s) in case auto-notification fails
- Auto-notification on bash completion is the primary wake signal

Subagents can do parallel investigations (read files, run quick tests) but shouldn't own long jobs.

## 7. Defensive helpers for environment quirks at the boundary

Make tools work across env variations without user friction:
- `_resolve_binary(("ffprobe", "ffmpeg.ffprobe"))` -- both standard and snap-mangled binary names
- `normalize_path_input` -- Windows `D:\foo` translated to WSL `/mnt/d/foo` automatically
- `select_font_file` -- auto-pick CJK font when text contains CJK chars
- ffmpeg `-nostdin` on every invocation -- avoid stdin-eating in piped/heredoc contexts

Each helper was the result of a real failure (`ffprobe not on PATH`, `questionary returned a Windows path`, `watermark renders as boxes for Japanese text`, etc.). The helper removes the failure class.

## 8. Document non-obvious findings as docs in the repo

`HANDOFF.md` for project intent + spec. `TAKEAWAYS.md` for non-obvious technical findings (the concat demuxer bug, the snap fontconfig mess, the GPU benchmark numbers, why overlay_cuda was rejected). `WORKFLOW.md` for development patterns.

These are things that wouldn't be obvious from reading the code alone. Code knows WHAT; docs know WHY and WHY NOT.

## 9. Orchestrator-only with spawn subagents

Main session orchestrates; subagents do the coding. Benefits:
- Main session context stays small (no test output / file content cluttering it)
- Each subagent has a focused task, less drift
- Code review is a separate pass with fresh eyes (a code-reviewer subagent, not the implementer)
- Verification is independent of implementation

Trade-off: each spawn has overhead. For one-line edits, overhead > benefit; do directly. For real implementation tasks, the discipline pays off.

## 10. Preview before destructive action

For ops that modify files (move, concat, watermark batch):
- Show a preview of intended actions before execution
- Prompt for confirmation (with `--yes` to skip for scripts)
- Per-action labels in preview (`move / overwrite / skip-conflict / skip-self / skip-batch-collision`)

Costs nothing on the happy path; saves recovery situations when the user typed the wrong source or pattern.

## 11. Trim to 30s for fast smoke iteration

When testing video pipelines, trimming a real source to 30s with `-c copy` is instant. The 30s clip exercises the same code paths as the full 1-hour video, but encode tests run in seconds. Save the full-source run for one final verification pass.

## 12. Commit messages explain the WHY, not the WHAT

Conventional commit prefix (`feat:` / `fix:` / `docs:` / `chore:`) plus a body that explains the motivation, especially for non-obvious choices. Examples in this repo:

- `feat: add videos/watermark op (image+text, static/bounce/drift, CJK auto-detect, GPU encoder)` -- the body explains the snap-vs-apt trade-off, the CJK fallback chain, the comma-escape requirement, etc.
- `docs(takeaways): explain why overlay_cuda full-GPU pipeline path was rejected` -- the body links back to the apt 4.4.2 limitations.

Future-you (or a new contributor) reading `git log` should understand WHY each change happened.
