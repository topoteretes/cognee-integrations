"""The plugin update check, split out of _plugin_common.py to keep that file
under the plugin directory's per-file size limit.

Import it through _plugin_common, which re-exports every name here. Names
that live in _plugin_common are looked up there at call time (``_pc.<name>``),
so a monkeypatch on _plugin_common reaches this module too.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import _plugin_common as _pc

# --- Plugin update check (Phase 2) -------------------------------------------
# A background, hourly-guarded check comparing the installed plugin version against
# the version published on the marketplace's git ref. The network call runs only
# here (in the background idle watcher); the hot path (SessionStart message,
# status line) merely READS the marker file this writes — never the network.
# Talks only to raw.githubusercontent (CDN, no API rate limit) over the shared
# certifi TLS context. Opt out with COGNEE_UPDATE_CHECK=off.
_UPDATE_CHECK_FILE = _pc._PLUGIN_DIR / "update-check.json"
_UPDATE_CHECK_INTERVAL_DEFAULT = 3600.0
_UPDATE_DEFAULT_REPO = "topoteretes/cognee-integrations"
_UPDATE_DEFAULT_REF = "main"
_UPDATE_PLUGIN_ENTRY = "cognee-memory"
_KNOWN_MARKETPLACES = Path.home() / ".claude" / "plugins" / "known_marketplaces.json"
# Claude Code's install registry: rewritten the moment `/plugin update` (or a
# marketplace auto-update) installs a new version — unlike the version-pinned
# plugin dirs, it reflects an update in the same session it happened in.
_INSTALLED_PLUGINS_FILE = Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def _update_check_enabled() -> bool:
    return os.environ.get("COGNEE_UPDATE_CHECK", "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


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


def _update_source() -> Optional[tuple]:
    """Return (repo, ref) to read the published version from, or None to skip.

    Reads the 'cognee' marketplace source from Claude's known_marketplaces.json.
    A non-github source (e.g. a local-path dev install) returns None so local
    checkouts never nag themselves; a missing/odd file falls back to the default
    repo (the normal published case).
    """
    try:
        data = json.loads(_KNOWN_MARKETPLACES.read_text(encoding="utf-8"))
        entry = data.get("cognee") if isinstance(data, dict) else None
        source = entry.get("source") if isinstance(entry, dict) else None
        if isinstance(source, dict):
            if source.get("source") == "github" and source.get("repo"):
                return str(source["repo"]), str(source.get("ref") or _UPDATE_DEFAULT_REF)
            return None  # non-github (local path / other): skip, don't self-nag
    except FileNotFoundError:
        pass
    except Exception as exc:
        _pc.hook_log("update_source_read_failed", {"error": str(exc)[:200]})
    return _UPDATE_DEFAULT_REPO, _UPDATE_DEFAULT_REF


def _fetch_published_version(repo: str, ref: str, etag: str) -> tuple:
    """GET the raw marketplace.json and read the plugin entry's version.

    Returns (version, new_etag, error). version is '' on 304/not-found/error so
    the caller keeps the previously-known latest.
    """
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/.claude-plugin/marketplace.json"
    req = urllib.request.Request(url, method="GET")
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=5.0, context=_pc._https_context()) as resp:
            body = resp.read().decode("utf-8")
            new_etag = resp.headers.get("ETag", "") or etag
            data = json.loads(body)
            plugins = data.get("plugins", []) if isinstance(data, dict) else []
            for plugin in plugins:
                if isinstance(plugin, dict) and plugin.get("name") == _UPDATE_PLUGIN_ENTRY:
                    return str(plugin.get("version") or ""), new_etag, ""
            return "", new_etag, "entry_not_found"
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return "", etag, ""  # unchanged since last check
        return "", etag, f"http_{exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return "", etag, str(exc)[:120]


def maybe_check_for_update() -> None:
    """Background, ≤hourly update check. Writes the marker. Never raises.

    Call from a background process (the idle watcher) — never a synchronous hook,
    since it may make a network call (bounded to 5s, ≤ once per interval).
    """
    try:
        if not _update_check_enabled():
            return
        marker = _pc._load_json_file(_UPDATE_CHECK_FILE)
        interval = _pc._float_env("COGNEE_UPDATE_CHECK_INTERVAL", _UPDATE_CHECK_INTERVAL_DEFAULT)
        now = datetime.now(timezone.utc).timestamp()
        if now - float(marker.get("last_checked_at", 0) or 0) < interval:
            return  # checked recently
        source = _update_source()
        if source is None:
            return  # local-dev / unknown source
        repo, ref = source
        installed = _pc._installed_plugin_version()
        latest, etag, error = _fetch_published_version(repo, ref, str(marker.get("etag") or ""))
        if not latest:
            latest = str(marker.get("latest_version") or "")  # 304/error: keep prior
        update_available = bool(installed and latest and _semver_gt(latest, installed))
        _pc._write_json_file(
            _UPDATE_CHECK_FILE,
            {
                "last_checked_at": now,
                "installed_version": installed,
                "latest_version": latest,
                "update_available": update_available,
                "etag": etag,
                "source": f"{repo}@{ref}",
                "error": error,
                # preserved so the SessionStart message shows once per new version
                "notified_version": str(marker.get("notified_version") or ""),
            },
        )
        _pc.hook_log(
            "update_check",
            {
                "installed": installed,
                "latest": latest,
                "available": update_available,
                "error": error,
            },
        )
    except Exception as exc:
        _pc.hook_log("update_check_failed", {"error": str(exc)[:200]})


def read_update_status() -> dict:
    """Zero-network read of the update marker for surfacing.

    Returns the marker dict only when an update is genuinely available; {} when
    disabled, absent, or already current (so callers can treat truthiness as
    "should nudge").

    The marker is a snapshot from the last background check, so it goes stale the
    moment the plugin is updated — it would keep claiming an update is available
    until the next check (≤ an hour later). Guard against that here, at read time:
    the snapshot is only trustworthy while its ``installed_version`` is still the
    version actually running. A mismatch means the update already landed, so the
    nudge is suppressed immediately rather than after the next network check.
    Two suppression signals, either one clears the nudge:
    - the running version moved past the marker's ``installed_version`` (the
      session restarted into the new copy), or
    - Claude Code's install registry (``installed_plugins.json``) already
      records a version at or past ``latest_version`` — `/plugin update`
      rewrites it immediately, so this clears the nudge in the very session
      the update happened in, even though that session still RUNS the old
      copy (installs are version-pinned dirs and only a restart re-points
      the hooks).
    """
    if not _update_check_enabled():
        return {}
    marker = _pc._load_json_file(_UPDATE_CHECK_FILE)
    if not (
        isinstance(marker, dict)
        and marker.get("update_available")
        and marker.get("installed_version")
        and marker.get("latest_version")
    ):
        return {}
    # An undeterminable running version falls back to trusting the marker, so a
    # missing/unreadable plugin.json degrades to the previous behaviour.
    running = _pc._installed_plugin_version()
    if running and running != marker.get("installed_version"):
        return {}
    recorded = _parse_semver(_recorded_install_version())
    published = _parse_semver(str(marker.get("latest_version") or ""))
    if recorded and published and recorded >= published:
        return {}
    return marker


def mark_update_notified(version: str) -> None:
    """Record that the one-time SessionStart message for `version` has been shown."""
    try:
        marker = _pc._load_json_file(_UPDATE_CHECK_FILE)
        if not marker:
            return
        marker["notified_version"] = str(version or "")
        _pc._write_json_file(_UPDATE_CHECK_FILE, marker)
    except Exception as exc:
        _pc.hook_log("update_notified_write_failed", {"error": str(exc)[:200]})
