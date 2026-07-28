#!/usr/bin/env python3
"""Render the Cognee status line (Codex).

Invoked via ``cognee-statusline.sh``, which pipes a JSON context on stdin.
Deliberately standalone and pure-local: reads only env vars and
``~/.cognee-plugin/config.json`` — no network calls, no ``_plugin_common``
import.

Output: ``cognee: <dataset-name> · local`` or ``cognee: <dataset-name> · cloud``
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

_SHARED_ROOT = Path.home() / ".cognee-plugin"
_CONFIG_PATH = _SHARED_ROOT / "config.json"
_SERVER_READY_PATH = _SHARED_ROOT / "server-ready.json"
_BREAKER_PATH = _SHARED_ROOT / "recall-breaker.json"
_UPDATE_CHECK_PATH = _SHARED_ROOT / "codex" / "update-check.json"
_LLM_STATE_PATH = _SHARED_ROOT / "codex" / "llm-state.json"
# Per-session copies (see _plugin_common._write_session_marker): the shared files
# above are coordination state, these are what THIS terminal observed.
_LLM_STATE_DIR = _SHARED_ROOT / "codex" / "llm-state"
_CONN_STATE_DIR = _SHARED_ROOT / "codex" / "conn-state"

# TTL for an LLM-key verdict. The marker is machine-wide and refreshed only when an
# idle watcher LAUNCHES (session start, or a prompt that finds no live watcher) —
# there is no periodic re-check — so a verdict this old came from a session that is
# gone. Treat it as unknown rather than keep flagging a key the user may have fixed.
_LLM_STATE_STALE_SECONDS = 30 * 60
_DEFAULT_DATASET = "agent_sessions"


def _active_dataset() -> str:
    # 1. env var (inherited from the shell that launched Codex)
    v = os.environ.get("COGNEE_PLUGIN_DATASET", "").strip()
    if v:
        return v
    # 2. default
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


_FAIL_STATES = ("auth_failed", "unreachable", "server_error")


def _active_base_url() -> str:
    """Normalized base_url for this session (env, then config); '' in local mode."""
    url = os.environ.get("COGNEE_BASE_URL", "").strip()
    if not url:
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                url = str(data.get("base_url") or "").strip()
        except Exception:
            pass
    return url.rstrip("/")


def _read_json(path: Path) -> dict:
    """Parse a marker file into a dict; {} on anything unreadable. Never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _path_safe(session_id: str) -> bool:
    """The id comes from the host over stdin — never build a path from it unchecked."""
    return bool(session_id) and all(c.isalnum() or c in "._-" for c in session_id)


def _checked_at(marker: dict) -> float:
    try:
        return float(marker.get("checked_at") or marker.get("ready_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def _connection_marker(session_id: str) -> dict:
    """This terminal's own connection record, or the best available substitute.

    Sessions can genuinely disagree (different COGNEE_API_KEY against the same
    base_url), so a record another session wrote says nothing about this one.
    Resolution:
      1. our own per-session record, EXCEPT when the shared marker carries a
         FRESHER failure — the server is shared, so a just-observed outage applies
         to everyone and beats our older "ready";
      2. no record of our own → the shared marker, but only when it is
         unattributed (pre-upgrade writer, or a write before the session key was
         known); an attributed record belonging to someone else is ignored;
      3. nothing usable → {} (renders no glyph, same as a warming server).
    """
    shared = _read_json(_SERVER_READY_PATH)
    mine = _read_json(_CONN_STATE_DIR / f"{session_id}.json") if _path_safe(session_id) else {}
    if not mine:
        marked = str(shared.get("session_key") or "")
        if session_id and marked and marked != session_id:
            return {}
        return shared
    if str(shared.get("state") or "") in _FAIL_STATES and _checked_at(shared) > _checked_at(mine):
        return shared
    return mine


def _health_prefix(session_id: str = "") -> str:
    """Server-connection glyph for THIS session, from local markers (no network).

    Precedence — we keep it green until we actually know it is red:
      1. a recorded failure state in the marker → ``✕ (<reason>)``
      2. an open recall breaker (repeated recall failure) → ``✕ (unreachable)``
      3. a "ready" marker → ``● ``
      4. otherwise (no marker / warming / different target) → no glyph
    The marker (``server-ready.json``) carries {state, base_url, ...}; a state is
    trusted only when its base_url matches this session's, so a local-ready marker
    never greens a cloud session (or vice versa).
    """
    marker = _connection_marker(session_id)

    marked_url = str(marker.get("base_url") or "").rstrip("/")
    active_url = _active_base_url()
    url_mismatch = bool(active_url and marked_url and active_url != marked_url)

    if marker and not url_mismatch:
        state = str(marker.get("state") or ("ready" if marker.get("ready_at") else ""))
        if state in _FAIL_STATES:
            return f"✕ ({state}) "
        if state == "ready":
            try:
                raw = json.loads(_BREAKER_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and float(raw.get("cooldown_until", 0) or 0) > time.time():
                    return "✕ (unreachable) "
            except Exception:
                pass
            return "● "

    try:
        raw = json.loads(_BREAKER_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and float(raw.get("cooldown_until", 0) or 0) > time.time():
            return "✕ (unreachable) "
    except Exception:
        pass
    return ""


def _update_segment() -> str:
    """Plain-text 'update available' segment, or '' — read purely from the marker.

    Codex surfaces status inside the model's context (not a terminal bar), so this
    stays plain text (no ANSI). The idle watcher's background check writes the
    marker; this remains network-free and free of any ``_plugin_common`` import.
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
    return f"  ⬆ Cognee update available {installed}→{latest}"


def _llm_prefix(session_id: str = "") -> str:
    """Plain-text 'LLM key' failure glyph, or '' — local mode only.

    LLM_API_KEY is only used by the local server, so this is suppressed in cloud
    mode. Both verdicts come from the background idle watcher, which resolves the
    key exactly as the server does: `not_set` = no key configured anywhere,
    `auth_failed` = key rejected by the provider. Distinct reasons from the
    server-connection ones so the two keys aren't confused. Plain text (no ANSI)
    since Codex injects the status into model context, not a terminal bar.

    Per terminal, because the answer genuinely differs per terminal: the key is
    resolved from the checking session's own environment, so one launch can have it
    exported and another not. We read THIS session's record
    (``llm-state/<session_key>.json``) first; the shared marker is consulted only
    when we have none of our own, and then only if it is unattributed — another
    terminal's "not_set" must not red a bar whose key is fine, and its "ok" must not
    green a bar whose key is missing.

    Verdicts expire (`_LLM_STATE_STALE_SECONDS`): the marker is shared by every
    session, so without a TTL one stale write could keep accusing a key that has
    since been fixed. The watcher refreshes it when it launches (session start, or
    a prompt that finds no live watcher), throttled to at most once per
    COGNEE_LLM_CHECK_INTERVAL — so an active session stays well inside the window,
    while a session quiet for longer simply drops the glyph until the next check.
    """
    if _active_mode() != "local":
        return ""
    marker = _read_json(_LLM_STATE_DIR / f"{session_id}.json") if _path_safe(session_id) else {}
    if not marker:
        # No verdict of our own yet: the shared marker only speaks for us when it is
        # unattributed. Another terminal's "ok" must not green a bar whose key is
        # missing, just as its "not_set" must not red one whose key is fine.
        marker = _read_json(_LLM_STATE_PATH)
        marked_key = str(marker.get("session_key") or "")
        if session_id and marked_key and session_id != marked_key:
            return ""
    try:
        if time.time() - float(marker.get("checked_at") or 0) > _LLM_STATE_STALE_SECONDS:
            return ""
    except (TypeError, ValueError):
        return ""
    state = str(marker.get("llm_state") or "")
    if state == "not_set":
        return "✕ (llm_no_key) "
    if state == "auth_failed":
        return "✕ (llm_auth_failed) "
    return ""


def _status_prefix(session_id: str = "") -> str:
    """The single leading glyph slot shared by the server- and LLM-key signals.

    One slot, by precedence — showing a ● next to an ✕ would read as
    contradictory:
      1. a server-connection failure wins: if we can't reach or authenticate
         against the server, its LLM key is not the actionable problem
      2. otherwise an LLM-key failure, which *replaces* the ● (the ``llm_*``
         reason already says the server side itself is fine)
      3. otherwise whatever the server signal is (``● `` or nothing).
    """
    server = _health_prefix(session_id)
    if server.startswith("✕"):
        return server
    return _llm_prefix(session_id) or server


def render_status_for_host(host_id: str) -> str:
    """Return the status string. ``host_id`` is this session's key, used to show only
    LLM-key verdicts written by this session (the marker is machine-wide)."""
    return (
        f"{_status_prefix(str(host_id or ''))}cognee: {_active_dataset()} · {_active_mode()}"
        f"{_update_segment()}"
    )


def main() -> None:
    # Windows defaults stdio to the locale code page (e.g. cp1252), which cannot
    # encode the status glyphs (●, ✕, ⬆); writing one raises UnicodeEncodeError
    # and exits non-zero. Force UTF-8 on both streams so this renderer stays
    # crash-free when invoked directly via cognee-statusline.sh. Kept inside
    # main() (not at module scope) because session-start.py and
    # session-context-lookup.py import render_status_for_host — a module-level
    # reconfigure would hijack the importer's stdout. Best-effort: a stream that
    # can't be reconfigured (e.g. a captured stdout under test) is left as-is.
    for _stream in (sys.stdin, sys.stdout):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    try:
        json.load(sys.stdin)  # consume stdin as required by the host
    except Exception:
        pass
    sys.stdout.write(
        f"{_status_prefix()}cognee: {_active_dataset()} · {_active_mode()}{_update_segment()}"
    )


if __name__ == "__main__":
    main()
