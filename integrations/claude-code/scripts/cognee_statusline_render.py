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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from _env_file import load_env_file

# ~/.cognee/.env is pure-local too, so loading it here keeps the renderer's
# no-network/no-_plugin_common contract while honoring one-time config.
load_env_file()

_SHARED_ROOT = Path.home() / ".cognee-plugin"
_CONFIG_PATH = _SHARED_ROOT / "claude-code" / "config.json"
_SERVER_READY_PATH = _SHARED_ROOT / "server-ready.json"
_BREAKER_PATH = _SHARED_ROOT / "recall-breaker.json"
_UPDATE_CHECK_PATH = _SHARED_ROOT / "claude-code" / "update-check.json"
_PIPELINE_HEALTH_PATH = _SHARED_ROOT / "pipeline-health.json"
_LLM_STATE_PATH = _SHARED_ROOT / "claude-code" / "llm-state.json"
_RECALL_PATH = _SHARED_ROOT / "claude-code" / "last_recall.json"
_RECALL_DIR = _SHARED_ROOT / "claude-code" / "recall"
_CREDITS_PATH = _SHARED_ROOT / "claude-code" / "credits.json"
# Per-session copies (see _plugin_common._write_session_marker): the shared files
# above are coordination state, these are what THIS terminal observed.
_LLM_STATE_DIR = _SHARED_ROOT / "claude-code" / "llm-state"
_CONN_STATE_DIR = _SHARED_ROOT / "claude-code" / "conn-state"
_DEFAULT_DATASET = "agent_sessions"
# Must match _plugin_common._DEFAULT_LOCAL_SERVICE_URL: the hooks stamp this URL into
# the markers this renderer compares against.
_DEFAULT_LOCAL_BASE_URL = "http://localhost:8011"

# TTL for an LLM-key verdict. The marker is machine-wide and refreshed only when an
# idle watcher LAUNCHES (session start, or a prompt that finds no live watcher) —
# there is no periodic re-check — so a verdict this old came from a session that is
# gone. Treat it as unknown rather than keep flagging a key the user may have fixed.
_LLM_STATE_STALE_SECONDS = 30 * 60

# Passive, app-closed-safe mitigation for the pipeline-health sweep (Layer 1, a
# Windows Scheduled Task) -- PushNotification (Layer 2) only fires while the app
# is open, so this is what lets Mike see a stuck-pipeline finding the INSTANT he
# next opens any terminal running the plugin, even after a period the app was
# closed. Older than this many seconds, treat the file as stale/unknown rather
# than showing a possibly-outdated warning -- the sweep runs every 2-5 minutes,
# so anything older than that means the sweep itself has stopped, which is its
# own separate (unmonitored-by-this-glyph) problem, not something to imply here.
_PIPELINE_HEALTH_STALE_SECONDS = 30 * 60

# TTL for the credits balance. Written per turn (async prompt hook), after
# improve/remember, and by the idle watcher every ~5 minutes — so a marker
# older than this means every writer has stopped (session over, watcher dead);
# hide the balance rather than show a number that no longer reflects spend.
_CREDITS_STALE_SECONDS = 15 * 60

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


def _active_dataset() -> str:
    # 1. env var (inherited from the shell that launched Claude Code)
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


# Where your memory actually lives is the one thing in this line worth a
# double-take — mistaking a local session for a cloud one (or the reverse) means
# writing to the wrong place — so the mode gets its own bold colour. Cyan/magenta
# deliberately: red/green/yellow are already spoken for by the health glyph and the
# amber warnings, and the two read distinctly on both light and dark terminals.
# Bold+colour together so a terminal that drops one still shows the other.
_MODE_STYLES = {"local": "\033[1;36m", "cloud": "\033[1;35m"}


def _mode_label() -> str:
    """The mode, styled. `_active_mode()` stays plain — it is also a control value."""
    mode = _active_mode()
    style = _MODE_STYLES.get(mode)
    return f"{style}{mode}\033[0m" if style else mode


_FAIL_STATES = ("auth_failed", "unreachable", "server_error")
# Of those, the ones that are a property of the SERVER rather than of the credential
# the observing session happened to use. Only these may cross session boundaries: if
# one terminal can't reach the server, neither can the others — but one terminal's
# rejected API key says nothing about anyone else's. Letting auth_failed cross put a
# red ✕ (incorrect_cognee_api_key) on a healthy local terminal for a few seconds
# whenever a keyless cloud terminal started up.
_SERVER_WIDE_FAIL_STATES = ("unreachable", "server_error")


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
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            url = str(data.get("base_url") or "").strip()
            if url:
                return url.rstrip("/")
    except Exception:
        pass
    return _DEFAULT_LOCAL_BASE_URL


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


# Colour policy for the connection slot. Green ● only when we actually know the server
# is up AND authenticated; red ✕ + reason when we know it is not — the reason travels
# inside the red so the whole verdict reads as one unit. The LLM-key failure is red too
# (see _llm_prefix); the two are told apart by the REASON — incorrect_cognee_api_key is
# the key this plugin uses to reach the server, incorrect_llm_api_key is the key the
# local server uses to reach the LLM. Bold is set alongside the colour so a terminal
# that drops one still shows the other.
_OK_STYLE = "\033[1;32m"  # bold green
_FAIL_STYLE = "\033[1;31m"  # bold red
_RESET = "\033[0m"


# What the user sees in place of the internal state name. The marker keeps its own
# vocabulary (`auth_failed`, `not_set`) for logs and diagnosis; the bar names the thing
# to go fix instead. Both LLM verdicts — no key at all, and a key the provider rejected
# — collapse to one label: the fix is the same either way, and `llm-state.json` still
# records which of the two it was.
_COGNEE_KEY_REASON = "incorrect_cognee_api_key"
_LLM_KEY_REASON = "incorrect_llm_api_key"
_REASON_LABELS = {"auth_failed": _COGNEE_KEY_REASON}


def _ok_glyph() -> str:
    """The healthy dot, styled, with the trailing space the bar concatenates on."""
    return f"{_OK_STYLE}●{_RESET} "


def _fail_glyph(reason: str) -> str:
    """A failure and its reason, styled as one red unit (server *and* LLM key).

    The reason is what the user must go fix — `incorrect_cognee_api_key` vs
    `incorrect_llm_api_key` — which is what now distinguishes the two failure classes
    from each other, since both render red.
    """
    return f"{_FAIL_STYLE}✕ ({reason}){_RESET} "


def _url_mismatch(active_url: str, marked_url: str) -> bool:
    """True only when the marker is PROVABLY about a different server.

    Mirror image of ``_plugin_common.same_connection_target`` (which the hooks use to
    decide whether to record a failure). The two must stay equivalent, or a hook and
    this renderer would disagree about whether a marker applies. Kept as its own copy
    because this module is deliberately standalone — no ``_plugin_common`` import.
    """
    return bool(active_url and marked_url and active_url != marked_url)


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
    url_mismatch = _url_mismatch(active_url, marked_url)

    if marker and not url_mismatch:
        # Legacy marker (no 'state', has ready_at) reads as ready.
        state = str(marker.get("state") or ("ready" if marker.get("ready_at") else ""))
        if state in _FAIL_STATES:
            return _fail_glyph(_REASON_LABELS.get(state, state))
        if state == "ready":
            # Breaker override: recall is failing repeatedly even if the last
            # readiness write still says ready — that means we now know it's red.
            try:
                raw = json.loads(_BREAKER_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and float(raw.get("cooldown_until", 0) or 0) > time.time():
                    return _fail_glyph("unreachable")
            except Exception:
                pass
            return _ok_glyph()

    # No usable marker: fall back to the breaker as the only failure signal.
    try:
        raw = json.loads(_BREAKER_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and float(raw.get("cooldown_until", 0) or 0) > time.time():
            return _fail_glyph("unreachable")
    except Exception:
        pass
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
    # Marker staleness guard, mirroring _plugin_common.read_update_status: the
    # marker is a snapshot from the last background check, so after an update it
    # keeps claiming an update is available until the next one runs. Since this
    # renders every refresh, comparing against the running version clears the
    # segment within one refresh instead of within an hour.
    running = _running_plugin_version()
    if running and running != installed:
        return ""
    return f"   \033[1;33m⬆ Cognee update available {installed}→{latest}\033[0m"


def _running_plugin_version() -> str:
    """Version of the plugin copy this renderer belongs to, or '' if unreadable.

    Deliberately does not import ``_plugin_common`` (see module docstring). The
    status line is not a hook, so ``CLAUDE_PLUGIN_ROOT`` is usually unset; the
    install path is version-pinned (``.../cognee-memory/<version>/scripts/``), so
    this file's own location identifies the running version.
    """
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


def _pipeline_health_glyph() -> str:
    """ "⚠ N " when the pipeline sweep (scripts/pipeline_sweep.py) has a fresh,
    non-stale finding of one or more stuck runs or a down server; "" otherwise
    (no file yet, stale, or everything's clean). See
    docs/KB/pipeline-monitor-notify-policy.md for the full monitoring design this
    is one small passive piece of.
    """
    try:
        raw = json.loads(_PIPELINE_HEALTH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(raw, dict):
        return ""
    try:
        generated_at = datetime.fromisoformat(str(raw.get("generated_at", "")))
        age_seconds = (datetime.now(timezone.utc) - generated_at).total_seconds()
        if age_seconds > _PIPELINE_HEALTH_STALE_SECONDS:
            return ""
    except (ValueError, TypeError):
        return ""
    server = raw.get("server") or {}
    if server.get("up") is False:
        return "⚠ server-down "
    summary = raw.get("summary") or {}
    worst = str(summary.get("worst_classification") or "ok")
    flagged = (
        sum((summary.get("by_classification") or {}).values())
        if isinstance(summary.get("by_classification"), dict)
        else 0
    )
    if worst in ("alert", "critical") and flagged > 0:
        return f"⚠ {flagged} pipeline(s) stuck "
    return ""


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


def _llm_prefix(session_id: str = "") -> str:
    """Amber 'LLM key' failure glyph, or '' — local mode only, read from marker.

    LLM_API_KEY is only used by the local server, so this is suppressed in cloud
    mode. Both verdicts come from the background idle watcher, which resolves the
    key exactly as the server does: `not_set` = no key configured anywhere,
    `auth_failed` = key rejected by the provider. Distinct reasons from the
    server-connection ones so the two keys aren't confused. Amber (`\\033[1;33m`)
    so a broken LLM key reads differently at a glance from an uncolored
    server-connection failure; the reset lands before the trailing space so no
    color bleeds into the rest of the bar.

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
        return _fail_glyph(_LLM_KEY_REASON)
    return ""


def _recall_segment(session_id: str) -> str:
    """Dim 'what memory did on the last turn' counts, or '' — read from the marker.

    ``session-context-lookup`` already writes ``last_recall.json`` on every prompt
    (hits per scope + what the previous turn persisted) precisely so the status line
    can show them; this is the Claude Code counterpart of the ``Cognee memory:
    recall …`` header Codex injects into model context.

    Per session: ``recall/<session_key>.json`` is this session's own copy, so with
    several terminals open each bar shows its own numbers. The machine-wide
    ``last_recall.json`` (written for ``cognee_plugin.py``) is the fallback for hooks
    that predate the per-session copy, and is only trusted when it is unattributed or
    stamped with our session — a neighbour's counts must never appear here.

    Faint (`\\033[2m`) so it sits below the health glyph and dataset in the visual
    hierarchy; the reset prevents color bleed.
    """
    if os.environ.get("COGNEE_STATUSLINE_COUNTS", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return ""
    marker = _read_json(_RECALL_DIR / f"{session_id}.json") if _path_safe(session_id) else {}
    if not isinstance(marker.get("hits"), dict):
        marker = _read_json(_RECALL_PATH)
        marked_key = str(marker.get("session_key") or "")
        if session_id and marked_key and session_id != marked_key:
            return ""
    hits = marker.get("hits")
    if not isinstance(hits, dict):
        return ""

    def _n(mapping, key) -> int:
        try:
            return int(mapping.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    recall = (
        f"{_n(hits, 'session')}s/{_n(hits, 'trace')}t/"
        f"{_n(hits, 'graph_context')}g/{_n(hits, 'session_context')}a"
    )
    saves = marker.get("saves_last_turn")
    if isinstance(saves, dict):
        saved = f"{_n(saves, 'prompt')}p/{_n(saves, 'trace')}t/{_n(saves, 'answer')}a"
        return f" \033[2m· recall {recall} · saved {saved}\033[0m"
    return f" \033[2m· recall {recall}\033[0m"


def _credits_segment() -> str:
    """Cloud credits balance + approximate cost of the last memory operation.

    Pure-local like everything here: reads only ``credits.json``, which the
    hooks/idle watcher keep fresh (see ``_plugin_common.refresh_credits``).
    Renders nothing unless ALL of: cloud mode, marker present with a numeric
    balance, marker fresh (``_CREDITS_STALE_SECONDS``), marker written for the
    server this session talks to, and not opted out. Balance is green —
    red once negative, which is exactly the state the user most needs to see
    (a negative balance is real unfunded spend). The last-op cost renders at
    normal weight and carries a ``~``: spend aggregates asynchronously
    server-side, so the delta is an attribution, not an invoice.
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
    # The marker is a MAP keyed by tenant id (several terminals can be on
    # different tenants at once); each entry carries the service base_url it
    # was observed under. Select OUR tenant's entry by that binding — an
    # old-format flat marker has no dict values with a matching base_url, so
    # it simply renders nothing until the first new-format refresh.
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
    color = "\033[32m" if remaining >= 0 else "\033[31m"
    sign = "-" if remaining < 0 else ""
    seg = f" · {color}credits: {sign}${abs(remaining):,.2f}\033[0m"
    last_op = entry.get("last_op")
    if isinstance(last_op, dict):
        label = str(last_op.get("label") or "").strip()
        cost = last_op.get("cost_usd")
        if label and isinstance(cost, (int, float)) and not isinstance(cost, bool):
            # Normal weight (like the `cognee: <dataset>` text), NOT faint like
            # the recall/saved counters: what the last operation cost is a
            # first-class signal, not diagnostics.
            seg += f" · last {label} ~${cost:,.2f}"
    return seg


def _status_prefix(session_id: str = "") -> str:
    """The single left glyph slot shared by the server- and LLM-key signals.

    One slot, by precedence — showing a green ● next to an ✕ would read as
    contradictory:
      1. a server-connection failure wins: if we can't reach or authenticate
         against the server, its LLM key is not the actionable problem
      2. otherwise an LLM-key failure, which *replaces* the green ● (the
         ``llm_*`` reason already says the server side itself is fine)
      3. otherwise whatever the server signal is (``● `` or nothing).
    """
    server = _health_prefix(session_id)
    # Membership, not startswith: the glyph is now preceded by its colour escape.
    if "✕" in server:
        return server
    return _llm_prefix(session_id) or server


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

    # Host session id: markers are per-integration, not per-session, so both the
    # LLM-key verdict and the recall counts are attributed before being shown.
    _session_id = str(ctx.get("session_id") or "")
    # The recall counts belong to the cognee core info, so they sit right after the
    # mode; the update nudge stays last because it is a transient banner, not part
    # of the steady-state line.
    sys.stdout.write(
        f"{_pipeline_health_glyph()}{_status_prefix(_session_id)}"
        f"cognee: {_active_dataset()} · {_mode_label()}"
        f"{_credits_segment()}{_recall_segment(_session_id)}{_update_segment()}"
    )


if __name__ == "__main__":
    main()
