"""Helpers shared by the standalone scripts in this directory.

Import as ``from scripts._common import ...`` after the script has put the
repository root on ``sys.path``; that works both when a script is run directly
and when a test imports it as ``scripts.<name>``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 5


def git_commit(root: Path) -> str | None:
    """Full HEAD commit hash of the repository at ``root``, or None if unknown."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            check=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


# Windows font files that cover Japanese, tried in order.
_JAPANESE_FONT_CANDIDATES = (
    Path(r"C:\Windows\Fonts\NotoSansJP-VF.ttf"),
    Path(r"C:\Windows\Fonts\meiryo.ttc"),
)


def japanese_font_properties(font_properties_type):
    """``font_properties_type(fname=...)`` for the first installed Japanese font, else None."""
    for path in _JAPANESE_FONT_CANDIDATES:
        if path.exists():
            return font_properties_type(fname=str(path))
    return None
