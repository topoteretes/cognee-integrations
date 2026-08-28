#!/usr/bin/env python3
"""Render the Cognee status line (Codex).

Invoked via ``cognee-statusline.sh``, which pipes a JSON context on stdin.
Deliberately standalone and pure-local: reads only env vars
(``~/.cognee/.env`` included) and the plugin's own state files — no network
calls, no ``_plugin_common`` import.

Output: ``cognee: <dataset-name> · local`` or ``cognee: <dataset-name> · cloud``
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from _env_file import forced_backend, load_env_file

# ~/.cognee/.env is pure-local too, so loading it here keeps the renderer's
# no-network/no-_plugin_common contract while honoring one-time config.
# load_env_file also applies the COGNEE_BACKEND switch (forced local scrubs the
# cloud connection vars), so the mode shown below matches what the hooks use.
load_env_file()

_SHARED_ROOT = Path.home() / ".cognee-plugin"
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
_CREDITS_PATH = _SHARED_ROOT / "codex" / "credits.json"

# TTL for the credits balance. Written per turn (prompt + Stop hooks) and by
# the idle watcher every ~5 minutes — older than this means every writer has
# stopped (session over, watcher dead); hide the balance rather than show a
# number that no longer reflects spend.
_CREDITS_STALE_SECONDS = 15 * 60

_DEFAULT_DATASET = "agent_sessions"
# Must match _plugin_common._DEFAULT_LOCAL_SERVICE_URL: the hooks stamp this URL into
# the markers this renderer compares against.
_DEFAULT_LOCAL_BASE_URL = "http://localhost:8011"


_SESSIONS_DIR = _SHARED_ROOT / "codex" / "sessions"


def _launch_record(host_id: str) -> dict:
    """This launch's record (``sessions/<host id>.json``), or {}.

    The host session key handed to the renderer is the same key SessionStart
    files the record under, so the bar reads the dataset the launch is actually
    writing to — including one chosen with the ``cognee-switch-datasets`` skill.
    """
    if not _path_safe(host_id):
        return {}
    return _read_json(_SESSIONS_DIR / f"{host_id}.json")


def _active_dataset(host_id: str = "") -> str:
    # 1. the launch record (authoritative once SessionStart has run; switchable)
    recorded = str(_launch_record(host_id).get("dataset") or "").strip()
    if recorded:
        return recorded
    # 2. env var (inherited from the shell that launched Codex)
    v = os.environ.get("COGNEE_PLUGIN_DATASET", "").strip()
    if v:
        return v
    # 3. default
    return _DEFAULT_DATASET


def _switched_marker(host_id: str = "") -> str:
    """A plain ``· switched`` tag once the launch left its launch-time dataset
    (this bar goes into the model's context, so no styling)."""
    if _launch_record(host_id).get("switched_at"):
        return " · switched"
    return ""


_LOOPBACK = {"localhost", "127.0.0.1", "::1", ""}


def _active_mode() -> str:
    # 0. explicit backend switch: forced local always reads local (the env
    # scrub in load_env_file guarantees it, this is just the direct answer);
    # forced cloud with no URL to inspect reads cloud — the misconfig glyph
    # (see _forced_cloud_unconfigured) reports what is missing.
    forced = forced_backend()
    if forced == "local":
        return "local"
    url = os.environ.get("COGNEE_BASE_URL", "").strip()
    if not url:
        return "cloud" if forced == "cloud" else "local"
    return "local" if (urlparse(url).hostname or "") in _LOOPBACK else "cloud"


_FAIL_STATES = ("auth_failed", "unreachable", "server_error", "not_responding")
# Of those, the ones that are a property of the SERVER rather than of the credential
# the observing session happened to use. Only these may cross session boundaries: if
# one terminal can't reach the server, neither can the others — but one terminal's
# rejected API key says nothing about anyone else's. Letting auth_failed cross put a
# red ✕ (incorrect_cognee_api_key) on a healthy local terminal for a few seconds
# whenever a keyless cloud terminal started up.
# "not_responding" (N consecutive recall timeouts) is server-wide too: a server
# that isn't answering one terminal isn't answering the others either.
_SERVER_WIDE_FAIL_STATES = ("unreachable", "server_error", "not_responding")

# A failure verdict is only worth a ✕ while it is FRESH. The hooks refresh a
# genuine outage on every prompt (probe or recall attempt), so a failure marker
# older than this means no session has re-confirmed it — ambiguous, and ambiguity
# renders no glyph (same as the warming case) rather than a stale accusation.
_FAIL_STATE_STALE_SECONDS = 30 * 60


def _active_base_url() -> str:
    """The server URL this session is actually talking to — never empty.

    Mirrors the hooks' resolution (``_plugin_common._local_api_url_with_source``)
    exactly, including the ``COGNEE_LOCAL_API_URL`` precedence and the localhost
    default, because the value is compared against the ``base_url`` those hooks stamp
    into the markers. It used to return "" when nothing was configured — the common
    local setup — which made the mismatch guard below toothless: with no URL of our
    own to compare, a marker written for someone else's *cloud* tenant was accepted by
    a *local* session. Defaulting here is what gives that guard teeth.
    """
    for var in ("COGNEE_LOCAL_API_URL", "COGNEE_BASE_URL"):
        url = os.environ.get(var, "").strip()
        if url:
            return url.rstrip("/")
    return _DEFAULT_LOCAL_BASE_URL


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
      1. our own per-session record, EXCEPT when the shared marker carries a fresher
         SERVER-WIDE failure (`unreachable` / `server_error`) — the server is shared,
         so a just-observed outage applies to everyone and beats our older "ready".
         `auth_failed` is deliberately excluded: it describes the credential the other
         session used, not the server, so it must not turn our bar red;
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
    if str(shared.get("state") or "") in _SERVER_WIDE_FAIL_STATES and _checked_at(
        shared
    ) > _checked_at(mine):
        return shared
    return mine


# What the user sees in place of the internal state name — the marker keeps its own
# vocabulary (`auth_failed`, `not_set`) for logs; the status names the thing to go fix.
# Both LLM verdicts collapse to one label: the fix is the same either way, and
# `llm-state.json` still records which of the two it was.
_COGNEE_KEY_REASON = "incorrect_cognee_api_key"
_LLM_KEY_REASON = "incorrect_llm_api_key"
_MISSING_URL_REASON = "missing_cognee_base_url"
_REASON_LABELS = {"auth_failed": _COGNEE_KEY_REASON}


def _url_mismatch(active_url: str, marked_url: str) -> bool:
    """True only when the marker is PROVABLY about a different server.

    Mirror image of ``_plugin_common.same_connection_target`` (which the hooks use to
    decide whether to record a failure). The two must stay equivalent, or a hook and
    this renderer would disagree about whether a marker applies. Kept as its own copy
    because this module is deliberately standalone — no ``_plugin_common`` import.
    """
    return bool(active_url and marked_url and active_url != marked_url)


def _breaker_glyph(active_url: str) -> str:
    """ "✕ (<trip reason>) " when THIS server's breaker is open, else "".

    The breaker file is keyed by base_url (SDK-356): an entry for a different
    server — a cloud tenant while this terminal is local, or Claude Code's
    target — must not red this bar. A legacy flat file (machine-wide,
    target-blind) is ignored for the same reason. The reason travels from the
    trip site, so a breaker opened by 5xx reads ``server_error``, not a false
    ``unreachable``.
    """
    try:
        raw = json.loads(_BREAKER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    servers = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        return ""
    entry = servers.get(active_url.rstrip("/"))
    if not isinstance(entry, dict):
        return ""
    try:
        if float(entry.get("cooldown_until", 0) or 0) <= time.time():
            return ""
    except (TypeError, ValueError):
        return ""
    reason = str(entry.get("reason") or "unreachable")
    return f"✕ ({_REASON_LABELS.get(reason, reason)}) "


def _health_prefix(session_id: str = "") -> str:
    """Server-connection glyph for THIS session, from local markers (no network).

    Precedence — the ✕ is reserved for CONFIRMED, FRESH, DEFINITIVE failures:
      1. a fresh recorded failure state in the marker → ``✕ (<reason>)``
         (older than _FAIL_STATE_STALE_SECONDS → ambiguous → no glyph)
      2. an open recall breaker for THIS base_url → ``✕ (<trip reason>)``
      3. a "ready" marker → ``● ``
      4. otherwise (no marker / warming / stale / different target) → no glyph
    The marker (``server-ready.json``) carries {state, base_url, ...}; a state is
    trusted only when its base_url matches this session's, so a local-ready marker
    never greens a cloud session (or vice versa).
    """
    marker = _connection_marker(session_id)

    marked_url = str(marker.get("base_url") or "").rstrip("/")
    active_url = _active_base_url()
    url_mismatch = _url_mismatch(active_url, marked_url)

    if marker and not url_mismatch:
        state = str(marker.get("state") or ("ready" if marker.get("ready_at") else ""))
        if state in _FAIL_STATES:
            if time.time() - _checked_at(marker) <= _FAIL_STATE_STALE_SECONDS:
                return f"✕ ({_REASON_LABELS.get(state, state)}) "
            # Stale verdict: nobody has re-confirmed the failure — treat as
            # unknown rather than keep accusing a server that may be fine.
            return _breaker_glyph(active_url)
        if state == "ready":
            return _breaker_glyph(active_url) or "● "

    return _breaker_glyph(active_url)


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
    # Marker staleness guard, mirroring _plugin_common.read_update_status: the
    # marker is a snapshot from the last background check, so after an update it
    # keeps claiming an update is available until the next one runs. Comparing
    # against the running version clears the segment on the next render instead.
    running = _running_plugin_version()
    if running and running != installed:
        return ""
    return f"  ⬆ Cognee update available {installed}→{latest}"


def _running_plugin_version() -> str:
    """Version of the plugin copy this renderer belongs to, or '' if unreadable.

    Deliberately does not import ``_plugin_common`` (see module docstring); this
    file's own location identifies the running copy.
    """
    candidates = []
    root = os.environ.get("PLUGIN_ROOT", "").strip()
    if root:
        candidates.append(Path(root) / ".codex-plugin" / "plugin.json")
    candidates.append(Path(__file__).resolve().parent.parent / ".codex-plugin" / "plugin.json")
    for path in candidates:
        try:
            version = str(json.loads(path.read_text(encoding="utf-8")).get("version") or "").strip()
            if version:
                return version
        except Exception:
            continue
    return ""


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
    if state in ("not_set", "auth_failed"):
        return f"✕ ({_LLM_KEY_REASON}) "
    return ""


def _forced_cloud_unconfigured() -> bool:
    """Forced cloud (backend switch) with no URL anywhere: nothing to connect
    to — a definitive misconfiguration this renderer can see directly from
    the environment, without waiting for a hook to record a failed attempt.
    """
    if forced_backend() != "cloud":
        return False
    return not os.environ.get("COGNEE_BASE_URL", "").strip()


def _status_prefix(session_id: str = "") -> str:
    """The single leading glyph slot shared by the server- and LLM-key signals.

    One slot, by precedence — showing a ● next to an ✕ would read as
    contradictory:
      0. forced cloud with no URL configured: a misconfiguration this renderer
         can prove on its own — the precise reason beats any marker-derived one
      1. a server-connection failure wins: if we can't reach or authenticate
         against the server, its LLM key is not the actionable problem
      2. otherwise an LLM-key failure, which *replaces* the ● (the ``llm_*``
         reason already says the server side itself is fine)
      3. otherwise whatever the server signal is (``● `` or nothing).
    """
    if _forced_cloud_unconfigured():
        return f"✕ ({_MISSING_URL_REASON}) "
    server = _health_prefix(session_id)
    if server.startswith("✕"):
        return server
    return _llm_prefix(session_id) or server


def _credits_segment() -> str:
    """Cloud credits balance + approximate cost of the last memory operation.

    Pure-local like everything here: reads only ``credits.json`` — a MAP keyed
    by tenant id (several terminals can be on different tenants at once), each
    entry carrying the service base_url it was observed under. Select OUR
    tenant's entry by that binding. Plain text (the Codex line carries no ANSI
    styling). Renders nothing unless ALL of: cloud mode, matching fresh entry
    with a numeric balance, not opted out (``COGNEE_STATUSLINE_CREDITS=off``).
    """
    if os.environ.get("COGNEE_STATUSLINE_CREDITS", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return ""
    if _active_mode() != "cloud":
        return ""
    marker = _read_json(_CREDITS_PATH)
    active = _active_base_url().rstrip("/")
    entry = None
    for candidate in marker.values():
        if (
            isinstance(candidate, dict)
            and str(candidate.get("base_url") or "").rstrip("/") == active
        ):
            entry = candidate
            break
    if entry is None:
        return ""
    remaining = entry.get("remaining_usd")
    if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
        return ""
    try:
        checked_at = float(entry.get("checked_at", 0) or 0)
    except (TypeError, ValueError):
        return ""
    if time.time() - checked_at > _CREDITS_STALE_SECONDS:
        return ""
    sign = "-" if remaining < 0 else ""
    seg = f" · credits: {sign}${abs(remaining):,.2f}"
    last_op = entry.get("last_op")
    if isinstance(last_op, dict):
        label = str(last_op.get("label") or "").strip()
        cost = last_op.get("cost_usd")
        if label and isinstance(cost, (int, float)) and not isinstance(cost, bool):
            seg += f" · last {label} ~${cost:,.2f}"
    return seg


def render_status_for_host(host_id: str) -> str:
    """Return the status string. ``host_id`` is this session's key, used to show only
    LLM-key verdicts written by this session (the marker is machine-wide)."""
    return (
        f"{_status_prefix(str(host_id or ''))}"
        f"cognee: {_active_dataset(str(host_id or ''))} · {_active_mode()}"
        f"{_switched_marker(str(host_id or ''))}"
        f"{_credits_segment()}{_update_segment()}"
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

    ctx: dict = {}
    try:
        ctx = json.load(sys.stdin)  # consume stdin as required by the host
    except Exception:
        ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}
    # The host session id (when the context carries one) selects this launch's
    # record, so the dataset shown follows a switch.
    host_id = str(ctx.get("session_id") or ctx.get("thread_id") or "")
    sys.stdout.write(
        f"{_status_prefix()}"
        f"cognee: {_active_dataset(host_id)} · {_active_mode()}{_switched_marker(host_id)}"
        f"{_credits_segment()}{_update_segment()}"
    )


if __name__ == "__main__":
    main()
