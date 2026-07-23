#!/usr/bin/env python3
"""Render the Cognee status line.

Invoked by Claude Code's ``statusLine`` (via ``cognee-statusline.sh``), which
pipes a JSON context on stdin. Deliberately standalone and pure-local: reads
only env vars and ``~/.cognee-plugin/config.json`` — no network calls, no
``_plugin_common`` import.

Output: ``cognee: <dataset-name> · local`` or ``cognee: <dataset-name> · cloud``
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

_SHARED_ROOT = Path.home() / ".cognee-plugin"
_CONFIG_PATH = _SHARED_ROOT / "claude-code" / "config.json"
_SERVER_READY_PATH = _SHARED_ROOT / "server-ready.json"
_BREAKER_PATH = _SHARED_ROOT / "recall-breaker.json"
_UPDATE_CHECK_PATH = _SHARED_ROOT / "claude-code" / "update-check.json"
_DEFAULT_DATASET = "agent_sessions"
# Cap for the untrusted project picker file (see config.py _read_project_picker).
_PICKER_MAX_BYTES = 64 * 1024

# Self-eviction: when the plugin is uninstalled/disabled but its files still
# linger in the version cache (Claude Code does not remove the statusLine key we
# wrote into ~/.claude/settings.json, and may keep the cached script on disk),
# this renderer would otherwise keep drawing a status line for a plugin that is
# no longer active. On each run we check whether the plugin is still enabled in
# any settings scope; if not, we remove our own statusLine entry and render
# nothing. SessionStart re-adds it whenever the plugin is genuinely active, so a
# transient mismatch self-heals on the next launch.
_PLUGIN_ID = "cognee-memory@cognee"
_USER_SETTINGS = Path.home() / ".claude" / "settings.json"
# A statusLine we consider "ours" to evict — never touch a user's own line.
_OWNED_STATUSLINE_MARKER = "cognee-statusline"


def _active_dataset(cwd: str = "") -> str:
    # 1. env var (inherited from the shell that launched Claude Code)
    v = os.environ.get("COGNEE_PLUGIN_DATASET", "").strip()
    if v:
        return v
    # 2. project picker (.cognee/session-config.json). Mirror config.py's root
    # resolution (cwd > CLAUDE_CWD > getcwd) so the displayed dataset matches
    # what the hooks resolve. The global config file is deliberately not read
    # here — like load_config, it does not drive the dataset (visibility-only).
    try:
        root = cwd or os.environ.get("CLAUDE_CWD") or os.getcwd()
        p = Path(root) / ".cognee" / "session-config.json"
        # Untrusted committed file: skip symlinks and cap the size, mirroring
        # config.py's _read_project_picker.
        if not p.is_symlink() and p.is_file() and p.stat().st_size <= _PICKER_MAX_BYTES:
            picker = json.loads(p.read_text("utf-8"))
            if isinstance(picker, dict):
                v = str(picker.get("dataset") or "").strip()
                if v:
                    return v
    except Exception:
        pass
    # 3. default
    return _DEFAULT_DATASET


_LOOPBACK = {"localhost", "127.0.0.1", "::1", ""}


def _active_mode() -> str:
    # 1. env var
    url = os.environ.get("COGNEE_BASE_URL", "").strip()
    # 2. config file
    if not url:
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                url = str(data.get("base_url") or "").strip()
        except Exception:
            pass
    if not url:
        return "local"
    return "local" if (urlparse(url).hostname or "") in _LOOPBACK else "cloud"


def _health_prefix() -> str:
    try:
        raw = json.loads(_BREAKER_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            if float(raw.get("cooldown_until", 0) or 0) > time.time():
                return "✕ "
    except Exception:
        pass
    if _SERVER_READY_PATH.exists():
        return "● "
    return ""


def _update_segment() -> str:
    """Amber 'update available' segment, or '' — read purely from the marker.

    The background idle watcher writes the marker; this stays network-free and
    plugin-runtime-free (no ``_plugin_common`` import), consistent with the
    renderer's pure-local design. Uses `\\033[1;33m` (bold + amber); terminals
    that ignore bold still apply the amber, and the trailing reset prevents
    color bleed into the rest of the bar.
    """
    if os.environ.get("COGNEE_UPDATE_CHECK", "").strip().lower() in ("0", "false", "no", "off"):
        return ""
    try:
        marker = json.loads(_UPDATE_CHECK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not (isinstance(marker, dict) and marker.get("update_available")):
        return ""
    installed = str(marker.get("installed_version") or "")
    latest = str(marker.get("latest_version") or "")
    if not (installed and latest):
        return ""
    return f"   \033[1;33m⬆ Cognee update available {installed}→{latest}\033[0m"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _enabled_in(path: Path):
    """Tri-state: True/False if the plugin key is present in this settings file,
    else None when the key is absent (file missing or no such entry)."""
    enabled = _read_json(path).get("enabledPlugins")
    if isinstance(enabled, dict) and _PLUGIN_ID in enabled:
        return bool(enabled[_PLUGIN_ID])
    return None


def _plugin_enabled(cwd: str) -> bool:
    """True if the plugin is enabled in ANY visible settings scope.

    Checks user settings plus the current project's settings/settings.local.
    Enabled if any scope has it truthy. If no scope lists it as truthy — i.e.
    disabled everywhere, or the entry was removed on uninstall — treat as not
    enabled so the renderer self-evicts.
    """
    paths = [_USER_SETTINGS]
    if cwd:
        try:
            proj = Path(cwd)
            paths += [proj / ".claude" / "settings.json", proj / ".claude" / "settings.local.json"]
        except Exception:
            pass
    return any(_enabled_in(p) is True for p in paths)


def _evict_own_statusline() -> None:
    """Remove our statusLine entry from user settings (best-effort, never raises).

    Only removes it when the entry is recognizably ours, so a status line the
    user set themselves is never touched. Atomic replace to avoid a torn file.
    """
    try:
        settings = _read_json(_USER_SETTINGS)
        sl = settings.get("statusLine")
        cmd = sl.get("command", "") if isinstance(sl, dict) else ""
        if _OWNED_STATUSLINE_MARKER not in str(cmd):
            return  # not ours (or already gone) — leave it alone
        settings.pop("statusLine", None)
        tmp = _USER_SETTINGS.with_name(f".settings-{os.getpid()}.json.tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, _USER_SETTINGS)
    except Exception:
        pass


def main() -> None:
    # Windows defaults stdio to the locale code page (e.g. cp1252), which cannot
    # encode the status glyphs (●, ✕, ⬆) — the write raises UnicodeEncodeError,
    # the renderer exits non-zero, and Claude Code drops the whole status line
    # (cp1252 also fails to decode a non-ASCII cwd from the JSON context). Force
    # UTF-8 on both streams: the context is UTF-8 and our output is UTF-8. Runtime
    # reconfigure overrides the inherited encoding; best-effort, since a stream
    # that can't be reconfigured (e.g. a captured stdout under test) is left as-is,
    # matching this renderer's never-raise design.
    for _stream in (sys.stdin, sys.stdout):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ctx = {}
    try:
        ctx = json.load(sys.stdin)  # consume stdin as required by Claude Code
    except Exception:
        ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}

    cwd = str(
        ctx.get("cwd")
        or (ctx.get("workspace") or {}).get("current_dir")
        or (ctx.get("workspace") or {}).get("project_dir")
        or ""
    )
    if not _plugin_enabled(cwd):
        # Plugin uninstalled/disabled but files linger: drop our own statusLine
        # entry and render nothing so the line disappears.
        _evict_own_statusline()
        return

    sys.stdout.write(
        f"{_health_prefix()}cognee: {_active_dataset(cwd)} · {_active_mode()}{_update_segment()}"
    )


if __name__ == "__main__":
    main()
