#!/usr/bin/env python3
"""Register the Cognee status line with the Cursor CLI (opt-in).

The Cursor CLI renders a status line above the prompt from ``statusLine`` in
``~/.cursor/cli-config.json`` (same contract as Claude Code's: a command spawned
on every conversation update, JSON context on stdin, stdout displayed). Nothing
else reads that key — the CLI ignores a ``statusLine`` in ``~/.claude/settings.json``
— so the plugin has to write it itself. SessionStart calls
:func:`ensure_statusline_configured` on every launch.

Opt-in, unlike the Claude Code plugin: a custom ``statusLine`` *replaces* the
CLI's own footer (model, approval state) instead of adding a row, and the file
belongs to the CLI, which rewrites it when the user changes a setting. So:

* nothing is written unless ``COGNEE_STATUSLINE=true``; with it unset (or
  false) our own entry, if an earlier opt-in left one, is removed again;
* a status line the user configured themselves is never replaced or removed;
* our own entry (recognised by ``cognee-statusline`` in the command) is updated
  in place when the plugin moves or the desired entry changes;
* ``COGNEE_STATUSLINE_TIMEOUT_MS`` and ``COGNEE_STATUSLINE_PADDING`` tune the
  CLI-side knobs;
* every edit is a read-modify-write under a lock shared by our writers, and is
  dropped (then retried once) when the file changed underneath it — the CLI
  does not take our lock, so a write of its own between our read and our
  replace must win, not be overwritten. The file is replaced atomically, every
  other key is preserved, a symlinked config (dotfiles) is written through to
  its target, and the file keeps its permissions.

The CLI spawns ``statusLine.command`` without a shell (``string-argv`` split,
``~`` expanded on the first token), so the command is the absolute path of the
launcher script, not a shell expression.

Only the Cursor CLI renders the line; the Cursor IDE has no status line.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import tempfile
from pathlib import Path
from typing import Callable

from _file_lock import file_lock

OWNED_MARKER = "cognee-statusline"
LAUNCHER_NAME = "cognee-statusline.sh"
_ENABLED = {"1", "true", "yes", "on"}
_DEFAULT_TIMEOUT_MS = 2000
_LOCK_TIMEOUT_SECONDS = 2.0

Logger = Callable[[str, dict], None]


class _ChangedUnderneath(Exception):
    """cli-config.json changed between our read and our replace."""


def _noop_log(_event: str, _detail: dict) -> None:
    return None


def cli_config_path() -> Path:
    return Path.home() / ".cursor" / "cli-config.json"


def statusline_enabled() -> bool:
    return os.environ.get("COGNEE_STATUSLINE", "").strip().lower() in _ENABLED


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


def _target(path: Path) -> Path:
    """The file to replace: a symlinked config is written through to its target,
    so the link (and the dotfiles repo behind it) survives."""
    try:
        return path.resolve() if path.is_symlink() else path
    except OSError:
        return path


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_mtime_ns, info.st_size


def _read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError("cli-config.json is not a JSON object")
    return loaded


def _write_atomic(path: Path, config: dict, expected: tuple[int, int] | None) -> None:
    target = _target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".cli-config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
        try:
            os.chmod(tmp, stat.S_IMODE(target.stat().st_mode))
        except FileNotFoundError:
            pass
        if _fingerprint(target) != expected:
            raise _ChangedUnderneath()
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _edit(path: Path, change: Callable[[dict], tuple[dict, str]]) -> str:
    """Read-modify-write ``path`` under our lock; returns ``change``'s outcome.

    ``change`` returns ``(new_config, outcome)``; the file is written only when
    the config differs. A concurrent write by the CLI is detected and the edit
    redone on the fresh contents once, then given up (``busy``).
    """
    target = _target(path)
    lock = target.with_name(f".{target.name}.cognee.lock")
    with file_lock(lock, timeout=_LOCK_TIMEOUT_SECONDS) as acquired:
        if not acquired:
            return "busy"
        for _attempt in range(2):
            expected = _fingerprint(target)
            config = _read_config(target)
            updated, outcome = change(config)
            if updated == config:
                return outcome
            try:
                _write_atomic(path, updated, expected)
                return outcome
            except _ChangedUnderneath:
                continue
        return "busy"


def _drop_owned(config: dict) -> tuple[dict, str]:
    if not is_owned(config.get("statusLine")):
        return config, "nothing_to_remove"
    updated = dict(config)
    updated.pop("statusLine", None)
    return updated, "removed"


def ensure_statusline_configured(
    hook_log: Logger = _noop_log,
    *,
    config_path: Path | None = None,
    plugin_root: str | os.PathLike[str] | None = None,
) -> str:
    """Write (or, when not opted in, remove) our ``statusLine``; never raises.

    Returns the outcome (``configured``, ``unchanged``, ``not_enabled``,
    ``removed``, ``script_not_found``, ``user_statusline_exists``, ``busy`` or
    ``failed``) for tests and logs it through ``hook_log``.
    """
    path = config_path or cli_config_path()
    try:
        if not statusline_enabled():
            outcome = _edit(path, _drop_owned) if path.exists() else "nothing_to_remove"
            if outcome == "removed":
                hook_log("statusline_removed", {"reason": "not_enabled"})
                return "removed"
            hook_log("statusline_setup_skipped", {"reason": "not_enabled"})
            return "not_enabled"

        launcher = launcher_path(plugin_root)
        if not launcher.is_file():
            hook_log(
                "statusline_setup_skipped", {"reason": "script_not_found", "path": str(launcher)}
            )
            return "script_not_found"

        desired = desired_entry(launcher)
        outcome = _edit(path, lambda config: merge(config, desired))
        if outcome in ("user_statusline_exists", "busy"):
            hook_log("statusline_setup_skipped", {"reason": outcome})
        elif outcome == "configured":
            hook_log("statusline_configured", {"path": str(launcher)})
        return outcome
    except Exception as exc:  # pragma: no cover - defensive, mirrors Claude Code
        hook_log("statusline_setup_failed", {"error": str(exc)[:200]})
        return "failed"


def remove_statusline(config_path: Path | None = None) -> bool:
    """Drop our own entry (never a user's); True when the file changed."""
    path = config_path or cli_config_path()
    try:
        return path.exists() and _edit(path, _drop_owned) == "removed"
    except Exception:
        return False


if __name__ == "__main__":  # manual: python3 _statusline_config.py [--remove]
    import sys

    if "--remove" in sys.argv[1:]:
        print("removed" if remove_statusline() else "nothing to remove")
    else:
        print(ensure_statusline_configured(lambda event, detail: print(event, detail)))
