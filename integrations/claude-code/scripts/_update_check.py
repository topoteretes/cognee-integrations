"""The plugin update check, split out of _plugin_common.py to keep that file
under the plugin directory's per-file size limit.

Import it through _plugin_common, which re-exports every name here. Names
that live in _plugin_common are looked up there at call time (``_pc.<name>``),
so a monkeypatch on _plugin_common reaches this module too.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import _plugin_common as _pc

# --- Installed plugin version ----------------------------------------------
# The plugin no longer checks for updates over the network (SDK-1411): Claude
# Code's own plugin updates cover directory and marketplace installs. What stays
# is reading which version is installed, from local files only.
_UPDATE_CHECK_FILE = _pc._PLUGIN_DIR / "update-check.json"
_UPDATE_PLUGIN_ENTRY = "cognee-memory"
_KNOWN_MARKETPLACES = Path.home() / ".claude" / "plugins" / "known_marketplaces.json"
# Claude Code's install registry: rewritten the moment `/plugin update` (or a
# marketplace auto-update) installs a new version — unlike the version-pinned
# plugin dirs, it reflects an update in the same session it happened in.
_INSTALLED_PLUGINS_FILE = Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def _parse_semver(value: str):
    """Parse the numeric X.Y.Z core (ignoring any -pre/+build suffix); None if not X.Y.Z."""
    core = str(value or "").strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _semver_gt(a: str, b: str) -> bool:
    pa, pb = _parse_semver(a), _parse_semver(b)
    return bool(pa and pb and pa > pb)


def _installed_plugin_version() -> str:
    candidates = []
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if root:
        candidates.append(Path(root) / ".claude-plugin" / "plugin.json")
    candidates.append(Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json")
    for path in candidates:
        try:
            version = str(json.loads(path.read_text(encoding="utf-8")).get("version") or "").strip()
            if version:
                return version
        except Exception:
            continue
    return ""


def _recorded_install_version() -> str:
    """Newest cognee-memory version recorded in Claude Code's install registry.

    ``installed_plugins.json`` maps ``<plugin>@<marketplace>`` to a list of
    installs. '' when the file is missing, unreadable, or has no parseable
    cognee-memory entry. Mirrored (not imported) by the status-line renderer,
    which stays free of this module.
    """
    try:
        data = json.loads(_INSTALLED_PLUGINS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return ""
    best, best_str = (), ""
    for key, installs in plugins.items():
        if str(key).split("@", 1)[0] != _UPDATE_PLUGIN_ENTRY:
            continue
        for install in installs if isinstance(installs, list) else []:
            version = str(install.get("version") or "") if isinstance(install, dict) else ""
            parsed = _parse_semver(version)
            if parsed and parsed > best:
                best, best_str = parsed, version
    return best_str


_UPDATE_DEFAULT_PLUGIN_ID = _UPDATE_PLUGIN_ENTRY + "@cognee"


def installed_plugin_id() -> str:
    """The ``<plugin>@<marketplace>`` id this install is registered under.

    The marketplace half depends on where the user installed from
    (``cognee-memory@cognee`` from this repo's marketplace,
    ``cognee-memory@anthropic-plugin-directory`` from the official directory),
    and ``/plugin update`` only accepts the id the install is actually
    registered under. Reads Claude Code's install registry; when several
    marketplaces have an entry, prefers the one holding the newest version.
    Falls back to the repo marketplace id when the registry is missing or
    has no cognee-memory entry.
    """
    try:
        data = json.loads(_INSTALLED_PLUGINS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _UPDATE_DEFAULT_PLUGIN_ID
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return _UPDATE_DEFAULT_PLUGIN_ID
    best_key, best = "", ()
    for key, installs in plugins.items():
        name, sep, marketplace = str(key).partition("@")
        if name != _UPDATE_PLUGIN_ENTRY or not sep or not marketplace:
            continue
        newest = ()
        for install in installs if isinstance(installs, list) else []:
            version = str(install.get("version") or "") if isinstance(install, dict) else ""
            parsed = _parse_semver(version)
            if parsed and parsed > newest:
                newest = parsed
        if not best_key or newest > best:
            best_key, best = str(key), newest
    return best_key or _UPDATE_DEFAULT_PLUGIN_ID
