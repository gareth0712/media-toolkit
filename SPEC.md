# SPEC — Cross-platform pipx release + README overhaul

Single source of truth for this change. Two parallel tasks (workflow/build, docs)
both follow this spec so they stay consistent without depending on each other.

## Goal

`media-toolkit` becomes a cross-platform "install and use" CLI via **pipx**, with:
1. An automated GitHub Actions **release** procedure (tag → build wheel → GitHub Release).
2. A README that surfaces **all commands at a glance** and gives a beginner-friendly
   **interactive walkthrough** (no need to memorize flags).

Out of scope (do NOT do): standalone PyInstaller binaries, test-CI badge, publishing
to PyPI, signing/notarization. ffmpeg stays an external system dependency.

## Why no build matrix

This package is **pure Python** (no compiled extensions). `python -m build` yields a
universal `py3-none-any` wheel + sdist that installs identically on Windows / macOS /
Linux. So a single Ubuntu build job is sufficient; pipx on any OS installs the same wheel.

## Release flow (canonical — both tasks must describe it identically)

1. Maintainer bumps `[project] version` in `pyproject.toml` (semver).
2. `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. GitHub Actions `release.yml` triggers on tag `v*`:
   - checkout → setup-python (3.11) → `pip install build` → `python -m build`
   - publish a GitHub Release for the tag, attaching `dist/*.whl` and `dist/*.tar.gz`.
4. End users install / upgrade with pipx (no repo clone needed):
   - `pipx install "git+https://github.com/gareth0712/media-toolkit.git@vX.Y.Z"`
   - or from a downloaded wheel: `pipx install ./media_toolkit-X.Y.Z-py3-none-any.whl`
   - upgrade: `pipx upgrade media-toolkit` (git source) or reinstall newer wheel.

This release bumps version `0.1.0 → 0.2.0` (new `videos split` feature = minor).

## ffmpeg per-OS (document in README Install)

| OS | Command |
|----|---------|
| macOS | `brew install ffmpeg` |
| Windows | `winget install ffmpeg` (or Gyan build) |
| Debian/Ubuntu | `sudo apt install ffmpeg` |

`ffprobe` ships with ffmpeg. Both must be on `PATH`.

## Task A — pyproject + release workflow (devops-engineer, Sonnet)

Files:
- `pyproject.toml`: bump `version` to `0.2.0`. Add nothing speculative. If a `dev`
  extra exists, optionally add `build` to it (only if it keeps things consistent).
- `.github/workflows/release.yml`: trigger `on: push: tags: ['v*']`. Single Ubuntu job:
  checkout (actions/checkout@v4), setup-python@v5 (3.11), `pip install build`,
  `python -m build`, then publish with `softprops/action-gh-release@v2` attaching
  `dist/*`. Needs `permissions: contents: write`. Pin action major versions.

Verification (zero-trust — paste actual output):
- Create a throwaway venv (do NOT pollute system Python):
  `python -m venv .build-venv` → `.build-venv/Scripts/python -m pip install build`
  → `.build-venv/Scripts/python -m build`. Confirm `dist/*.whl` + `dist/*.tar.gz` exist.
- Install the built wheel into the venv and confirm the console script works:
  `.build-venv/Scripts/python -m pip install dist/*.whl` then
  `.build-venv/Scripts/media-toolkit --help` (or `python -m media_toolkit --help`).
- Validate `release.yml` is syntactically valid YAML (parse it) and references the
  pinned actions above.
- Clean up: remove `.build-venv/` and `dist/` (do NOT commit them; ensure `.gitignore`
  covers `dist/` and the venv — add if missing, minimally).

## Task B — README overhaul (general-purpose, Sonnet)

Read these to document ACCURATE prompts/flags (do not invent):
- `media_toolkit/cli.py` (domain/op menu flow)
- `media_toolkit/videos/concat.py` → `register_subparser` + `interactive_args`
- `media_toolkit/videos/watermark.py` → same (note single-file vs directory/batch via
  `--input` + `--pattern`; note it always re-encodes; encoder auto/cpu/gpu)
- `media_toolkit/videos/split.py` → same (`--parts` vs `--segment-time`; stream-copy vs
  `--reencode`; the final method-select prompt + confirm)

README structure (keep existing good parts; concise, example-first, scannable):
1. Short intro (keep).
2. **Commands at a glance** — a compact table of all 3 video ops with one-line purpose
   + the single most common invocation each. Front-loaded near the top.
3. **Usage — interactive (PRIMARY section, make it excellent)**: this is what the user
   relies on. Show `media-toolkit` with no args, then a concrete walkthrough of the
   menu → for EACH op, the exact sequence of questions it asks and what a sensible
   answer looks like (use a fenced block mimicking the questionary prompts). Goal: a
   beginner can run every op without knowing any flag. Cover concat, watermark, split.
4. **Usage — subcommands** — keep/refresh the flag reference table for scripted use
   (all 3 ops, including split's two modes + `--reencode`, watermark's batch `--pattern`).
5. **Install** — pipx (install pipx per-OS link + `pipx install git+...@v0.2.0`) and the
   ffmpeg per-OS table above. Note Python 3.10+ requirement.
6. **Releasing** — the canonical release flow from this spec (bump version, tag, CI
   publishes Release; users `pipx install`/`upgrade`). Concise.
7. Keep Tests section.

Verification (paste actual output):
- `grep -nE "concat|watermark|split" README.md` shows all three documented.
- Confirm the interactive walkthrough exists for each op (grep for op names under an
  interactive heading).
- No invented flags: every flag mentioned must exist in the corresponding
  `register_subparser`. Spot-check by grepping the source for each flag you document.

## Integration verification (orchestrator, after both tasks)

- Re-run wheel build in a clean venv → `media-toolkit --help` works.
- `git status` shows only intended files (no `dist/`, no `.build-venv/`).
- README renders; release flow in README matches `release.yml`.

## Fallback

Branch `feat/videos-split` (already pushed). Per-file revert:
`git checkout -- <file>`. New files can be `git rm`. Nothing is force-pushed; the
prior commit `02bdacf` is the rollback point (`git reset --hard 02bdacf` to drop this
change entirely).

## Risks

- `python -m build` needs `build` installed — use an isolated venv for local verify so
  the system Python stays clean.
- `release.yml` cannot be fully verified without pushing a real tag (which cuts a real
  Release). Local build + YAML parse is the pre-merge gate; the FIRST real tag validates
  the workflow end-to-end. Document this; do NOT push a tag during this task.
- Avoid committing build artifacts (`dist/`, `*.egg-info` already ignored, venv).
