"""Path normalization helpers shared across ops.

The toolkit runs on WSL, where users frequently paste Windows-style
paths (e.g. ``D:\\Downloads\\wordup``) into prompts or CLI flags. Linux
``pathlib.Path`` treats those as relative paths, so we translate them
to the corresponding ``/mnt/<drive>/...`` form before resolution.
"""

from __future__ import annotations

import re
from pathlib import Path

WIN_DRIVE_PATTERN = re.compile(r"^([A-Za-z]):[\\/](.*)$", re.DOTALL)
WSL_MOUNT_PREFIX = "/mnt"


def normalize_path_input(value: str | Path | None) -> Path | None:
    """Convert a user-supplied path string into a Path, translating Windows
    paths to WSL form.

    - ``D:\\foo\\bar`` -> ``/mnt/d/foo/bar``
    - ``d:/foo`` -> ``/mnt/d/foo``
    - ``D:\\`` -> ``/mnt/d/``
    - ``/mnt/c/users`` -> unchanged
    - ``relative/sub`` -> ``Path("relative/sub")`` (left as-is)
    - ``None`` -> ``None`` (preserves "not yet supplied" semantics)
    - empty / whitespace-only -> ``None``
    - leading/trailing surrounding quotes (single OR double) are stripped
      before processing (some shells / questionary returns leave them in)
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Strip surrounding matched quotes
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1]
    match = WIN_DRIVE_PATTERN.match(text)
    if match:
        drive = match.group(1).lower()
        rest = match.group(2).replace("\\", "/")
        return Path(f"{WSL_MOUNT_PREFIX}/{drive}/{rest}")
    # Not a Windows absolute path -- defensively flip backslashes
    # in case user typed e.g. relative `sub\\dir`. Linux-native paths
    # don't contain backslashes, so this is safe.
    return Path(text.replace("\\", "/"))
