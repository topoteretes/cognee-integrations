"""Every file the Claude plugin directory reads stays under its per-file limit.

The directory holds a plugin for a reviewer when a file that is not an image or
a font is over 256 KiB ("Files or downloads the validator couldn't inspect").
``_plugin_common.py`` crossed it once by growing a little with each change; this
catches the next file that does before a submission is held for it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from utils.suites import CLAUDE

LIMIT_BYTES = 256 * 1024
EXEMPT_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
}


def _tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "."],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return [root / name for name in out.split("\0") if name]


def test_no_plugin_file_is_over_the_directory_limit():
    root = CLAUDE.plugin_manifest.parent.parent
    # Untracked new files count too: they ship once committed.
    files = set(_tracked_files(root)) | {p for p in (root / "scripts").glob("*.py")}
    over = sorted(
        (str(p.relative_to(root)), p.stat().st_size)
        for p in files
        if p.is_file()
        and p.suffix.lower() not in EXEMPT_SUFFIXES
        and p.stat().st_size > LIMIT_BYTES
    )
    assert over == [], f"files over {LIMIT_BYTES} bytes (split them): {over}"
