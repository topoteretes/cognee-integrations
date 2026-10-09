#!/usr/bin/env python3
"""Register the Cognee status line with the Cursor CLI.

The Cursor CLI renders a status line above the prompt from ``statusLine`` in
``~/.cursor/cli-config.json`` (same contract as Claude Code's: a command spawned
on every conversation update, JSON context on stdin, stdout displayed). Nothing
else reads that key — the CLI ignores a ``statusLine`` in ``~/.claude/settings.json``
— so the plugin has to write it itself. SessionStart calls
:func:`ensure_statusline_configured` on every launch.

Rules, matching the Claude Code plugin:

* a status line the user configured themselves is never replaced;
* our own entry (recognised by ``cognee-statusline`` in the command) is updated
  in place when the plugin moves or the desired entry changes;
* ``COGNEE_STATUSLINE=false`` opts out; ``COGNEE_STATUSLINE_TIMEOUT_MS`` and
  ``COGNEE_STATUSLINE_PADDING`` tune the CLI-side knobs;
* the file is rewritten atomically and every other key is preserved.

The CLI spawns ``statusLine.command`` without a shell (``string-argv`` split,
``~`` expanded on the first token), so the command is the absolute path of the
launcher script, not a shell expression.

Only the Cursor CLI renders the line; the Cursor IDE has no status line.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Callable

OWNED_MARKER = "cognee-statusline"
LAUNCHER_NAME = "cognee-statusline.sh"
_DISABLED = {"0", "false", "no", "off"}
_DEFAULT_TIMEOUT_MS = 2000

Logger = Callable[[str, dict], None]


def _noop_log(_event: str, _detail: dict) -> None:
    return None


def cli_config_path() -> Path:
    return Path.home() / ".cursor" / "cli-config.json"


def launcher_path(plugin_root: str | os.PathLike[str] | None = None) -> Path:
    """``scripts/cognee-statusline.sh`` of the plugin copy that is running.

    ``CURSOR_PLUGIN_ROOT`` is set by the hook adapter; outside a hook (tests, a
    manual run) fall back to this file's own plugin.
    """
    root = str(plugin_root or os.environ.get("CURSOR_PLUGIN_ROOT", "") or "").strip()
    if root:
        return Path(root) / "scripts" / LAUNCHER_NAME
    return Path(__file__).resolve().parent / LAUNCHER_NAME


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "") or default))
    except ValueError:
        return default


def desired_entry(launcher: Path) -> dict:
    """The ``statusLine`` object we want in cli-config.json."""
    entry: dict = {"type": "command", "command": shlex.quote(str(launcher))}
    timeout_ms = _int_env("COGNEE_STATUSLINE_TIMEOUT_MS", _DEFAULT_TIMEOUT_MS)
    if timeout_ms > 0 and timeout_ms != _DEFAULT_TIMEOUT_MS:
        entry["timeoutMs"] = timeout_ms
    padding = _int_env("COGNEE_STATUSLINE_PADDING", 0)
    if padding > 0:
        entry["padding"] = padding
    return entry


def is_owned(entry: object) -> bool:
    command = entry.get("command") if isinstance(entry, dict) else None
    return isinstance(command, str) and OWNED_MARKER in command


def merge(config: dict, desired: dict) -> tuple[dict, str]:
    """Return ``(new_config, outcome)``; outcome is one of
    ``unchanged`` / ``configured`` / ``user_statusline_exists``."""
    existing = config.get("statusLine")
    if existing == desired:
        return config, "unchanged"
    if existing and not is_owned(existing):
        return config, "user_statusline_exists"
    updated = dict(config)
    updated["statusLine"] = desired
    return updated, "configured"


def _write_atomic(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cli-config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_statusline_configured(
    hook_log: Logger = _noop_log,
    *,
    config_path: Path | None = None,
    plugin_root: str | os.PathLike[str] | None = None,
) -> str:
    """Write our ``statusLine`` into cli-config.json when safe; never raises.

    Returns the outcome (``configured``, ``unchanged``, ``disabled_by_env``,
    ``script_not_found``, ``user_statusline_exists`` or ``failed``) for tests
    and logs it through ``hook_log`` as the Claude Code plugin does.
    """
    if os.environ.get("COGNEE_STATUSLINE", "").strip().lower() in _DISABLED:
        hook_log("statusline_setup_skipped", {"reason": "disabled_by_env"})
        return "disabled_by_env"

    launcher = launcher_path(plugin_root)
    if not launcher.is_file():
        hook_log("statusline_setup_skipped", {"reason": "script_not_found", "path": str(launcher)})
        return "script_not_found"

    path = config_path or cli_config_path()
    try:
        config: dict = {}
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                loaded = json.loads(text)
                if not isinstance(loaded, dict):
                    raise ValueError("cli-config.json is not a JSON object")
                config = loaded
        merged, outcome = merge(config, desired_entry(launcher))
        if outcome == "user_statusline_exists":
            hook_log("statusline_setup_skipped", {"reason": outcome})
            return outcome
        if outcome == "configured":
            _write_atomic(path, merged)
            hook_log("statusline_configured", {"path": str(launcher)})
        return outcome
    except Exception as exc:  # pragma: no cover - defensive, mirrors Claude Code
        hook_log("statusline_setup_failed", {"error": str(exc)[:200]})
        return "failed"


def remove_statusline(config_path: Path | None = None) -> bool:
    """Drop our own entry (never a user's); True when the file changed."""
    path = config_path or cli_config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(config, dict) or not is_owned(config.get("statusLine")):
        return False
    config.pop("statusLine", None)
    _write_atomic(path, config)
    return True


if __name__ == "__main__":  # manual: python3 _statusline_config.py [--remove]
    import sys

    if "--remove" in sys.argv[1:]:
        print("removed" if remove_statusline() else "nothing to remove")
    else:
        print(ensure_statusline_configured(lambda event, detail: print(event, detail)))
