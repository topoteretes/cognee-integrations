#!/usr/bin/env python3
"""One-shot setup: register the Cognee status line in ~/.claude/settings.json.

Run once after installing the plugin:

    python3 /path/to/scripts/setup-statusline.py

The script resolves its own location, constructs the absolute path to
cognee-statusline.sh, and writes (or updates) the statusLine entry in
~/.claude/settings.json. Existing settings are preserved.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_SETTINGS = Path.home() / ".claude" / "settings.json"
_SCRIPT = Path(__file__).resolve().parent / "cognee-statusline.sh"


def _load_settings() -> dict:
    if not _SETTINGS.exists():
        return {}
    try:
        text = _SETTINGS.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else {}
    except json.JSONDecodeError as exc:
        print(f"error: {_SETTINGS} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)


def _save_settings(data: dict) -> None:
    _SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_SETTINGS.parent, prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, _SETTINGS)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> None:
    if not _SCRIPT.exists():
        print(f"error: cognee-statusline.sh not found at {_SCRIPT}", file=sys.stderr)
        sys.exit(1)

    settings = _load_settings()
    desired = {"type": "command", "command": str(_SCRIPT)}
    existing = settings.get("statusLine")

    if existing == desired:
        print(f"already set: statusLine -> {_SCRIPT}")
        return

    if existing and existing != desired:
        print(f"replacing existing statusLine:\n  was: {existing}\n  now: {desired['command']}")

    settings["statusLine"] = desired
    _save_settings(settings)
    print(f"done: statusLine set to {_SCRIPT}")
    print("Restart Claude Code for the change to take effect.")


if __name__ == "__main__":
    main()
