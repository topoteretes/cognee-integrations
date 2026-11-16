"""The scripts' version constant equals the manifest's version (SDK-1411, Claude Code).

The plugin directory holds a plugin whose code can reach an image file. The
manifest names the listing icon, so a script that opens the manifest counts as
reaching it. The status line and the update check therefore take the version
from ``scripts/_version.py``, and no script opens the manifest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[3] / "claude-code"


def test_version_constant_matches_the_manifest():
    manifest = json.loads((_PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    source = (_PLUGIN / "scripts" / "_version.py").read_text(encoding="utf-8")
    constant = re.search(r'^PLUGIN_VERSION = "([^"]+)"$', source, re.MULTILINE)
    assert constant and constant.group(1) == manifest["version"]


def test_no_script_names_the_manifest_or_the_icon():
    offenders = []
    for path in sorted((_PLUGIN / "scripts").glob("*")):
        if path.suffix not in (".py", ".sh", ""):
            continue
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in ("plugin.json", "icon.png"):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == []
