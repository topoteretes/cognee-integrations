"""Reversible ownership of Claude Code's native auto-memory setting."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path

from _file_lock import file_lock
from _injection_ledger import atomic_json


def _paths():
    return (
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".cognee-plugin" / "claude-code" / "auto-memory.json",
    )


def _read(path):
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("settings must be a JSON object")
    return data


def _write_settings(path: Path, data: dict) -> None:
    """Rewrite the user's settings in place: follow a symlink to its target and keep
    the file's permissions, indentation and trailing newline."""
    target = path.resolve() if path.is_symlink() else path
    try:
        original = target.read_text(encoding="utf-8")
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        original, mode = "", None
    match = re.search(r"\n([ \t]+)\S", original)
    indent = (match.group(1) if match else 2) if "\n" in original.strip() or not original else None
    text = json.dumps(data, indent=indent)
    if not original or original.endswith("\n"):
        text += "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix="." + target.name + "-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def restore() -> bool:
    settings_path, marker_path = _paths()
    if not marker_path.exists():
        return False  # Nothing of ours to give back; leave ~/.claude untouched.
    with file_lock(settings_path.with_name(".cognee-settings.lock"), timeout=0.2) as acquired:
        if not acquired:
            return False
        marker = _read(marker_path)
        if not marker.get("owned"):
            return False
        settings = _read(settings_path)
        # A later explicit user choice wins, even when our marker remains.
        if settings.get("autoMemoryEnabled") is False:
            if marker.get("previous_present"):
                settings["autoMemoryEnabled"] = marker.get("previous_value")
            else:
                settings.pop("autoMemoryEnabled", None)
            _write_settings(settings_path, settings)
        marker_path.unlink(missing_ok=True)
        return True


def configure() -> str:
    """Return a notice once; leave invalid settings and explicit user choices alone."""
    if os.environ.get("COGNEE_DISABLE_AUTO_MEMORY", "").lower() in {"0", "false", "no", "off"}:
        return "Cognee restored the previous auto-memory setting." if restore() else ""
    settings_path, marker_path = _paths()
    with file_lock(settings_path.with_name(".cognee-settings.lock"), timeout=0.2) as acquired:
        if not acquired:
            return ""
        settings, marker = _read(settings_path), _read(marker_path)
        if settings.get("autoMemoryEnabled") is True:
            if marker.get("user_choice_reported"):
                return ""
            marker["user_choice_reported"] = True
            atomic_json(marker_path, marker)
            return "Cognee left your explicit autoMemoryEnabled=true setting unchanged."
        if settings.get("autoMemoryEnabled") is False:
            return ""
        if marker.get("owned"):
            # We set it and the user has since removed it: that is their choice.
            atomic_json(marker_path, {"owned": False, "user_choice_reported": True})
            return ""
        if "autoMemoryEnabled" not in settings and marker.get("user_choice_reported"):
            return ""
        # Persist the old value before editing settings so an interrupted write is recoverable.
        marker = {
            "owned": True,
            "previous_present": "autoMemoryEnabled" in settings,
            "previous_value": settings.get("autoMemoryEnabled"),
        }
        atomic_json(marker_path, marker)
        settings["autoMemoryEnabled"] = False
        _write_settings(settings_path, settings)
        return (
            "Cognee disabled Claude auto-memory starting next session; "
            "opt out with COGNEE_DISABLE_AUTO_MEMORY=false."
        )


def sync(cloud: bool) -> str:
    """Take over native memory in cloud mode; anywhere else give back what we changed."""
    if cloud:
        return configure()
    return "Cognee restored the previous auto-memory setting." if restore() else ""


def state() -> dict:
    settings_path, marker_path = _paths()
    try:
        settings, marker = _read(settings_path), _read(marker_path)
        return {
            "enabled": settings.get("autoMemoryEnabled", "default"),
            "plugin_owned": bool(marker.get("owned")),
            "environment_override": os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY", ""),
        }
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}
