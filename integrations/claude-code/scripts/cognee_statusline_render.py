#!/usr/bin/env python3
"""Render the Cognee status line.

Invoked by Claude Code's ``statusLine`` (via ``cognee-statusline.sh``), which
pipes a JSON context on stdin. Deliberately standalone and pure-local: reads
only env vars (``~/.cognee/.env`` included) and the plugin's own state files —
no network calls, no ``_plugin_common`` import.

Output: ``cognee: <dataset-name> · local`` or ``cognee: <dataset-name> · cloud``
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
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
_UPDATE_CHECK_PATH = _SHARED_ROOT / "claude-code" / "update-check.json"
# Claude Code's own install registry: rewritten the moment `/plugin update`
# (or a marketplace auto-update) installs a new version, which makes it the
# only signal that clears the update nudge mid-session (see _update_segment).
_INSTALLED_PLUGINS_PATH = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
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

# Credits balance age. The marker is written only when this machine does
# something billable (prompt start, turn end, remember, improve) — there is no
# background poll, so an idle terminal makes no billing calls. The balance
# cannot move from here while idle, so an old reading is still the right
# number for this session; past this age it carries an "Nm ago" hint so the
# user knows it predates the idle stretch. Hidden only once older than the
# marker's own prune horizon (`_plugin_common._CREDITS_ENTRY_MAX_AGE_SECONDS`).
_CREDITS_AGE_HINT_SECONDS = 15 * 60
_CREDITS_MAX_AGE_SECONDS = 7 * 24 * 3600
# A balance at or below this is "running out": the number turns red and the
# top-up link appears. Not zero — the cloud stops serving before the balance
# reaches zero (a 402 arrives with cents left), so zero is never observed.
_CREDITS_LOW_USD = 1.0
# Where to top up. Shown next to a low balance and next to a 402 refusal that
# arrived without any balance reading. The production billing page, hardcoded:
# the web frontend's host cannot be derived from the tenant's data-plane host
# (``tenant-<id>.aws.cognee.ai`` pairs with ``platform.cognee.ai``, but
# ``tenant-<id>.dev-aws.cognee.ai`` pairs with ``staging.cognee.ai`` — a lookup,
# not a rule). Staging/dev sessions set ``COGNEE_BILLING_URL``.
_BILLING_URL_DEFAULT = "https://platform.cognee.ai/billing"


def _billing_url() -> str:
    return os.environ.get("COGNEE_BILLING_URL", "").strip() or _BILLING_URL_DEFAULT


def _payment_required_op(entry: dict) -> str:
    """The operation the server last refused with HTTP 402, or "".

    The hooks stamp ``payment_required: {op, at}`` on the tenant's entry when a
    billable request comes back 402 and remove it on the next success. Stale
    notes (older than the marker's prune horizon) are ignored: nothing has been
    tried against this tenant for a week, so nothing is known about it.
    """
    note = entry.get("payment_required")
    if not isinstance(note, dict):
        return ""
    op = str(note.get("op") or "").strip()
    if not op:
        return ""
    try:
        if time.time() - float(note.get("at", 0) or 0) > _CREDITS_MAX_AGE_SECONDS:
            return ""
    except (TypeError, ValueError):
        return ""
    return op


def _credits_age_hint(age_seconds: float) -> str:
    """``"16m ago"`` / ``"3h ago"`` / ``"2d ago"`` once a reading is older than
    ``_CREDITS_AGE_HINT_SECONDS``; ``""`` while it is recent enough to read as
    current. Coarse on purpose — it says "this number predates your idle
    stretch", not a timestamp."""
    if age_seconds <= _CREDITS_AGE_HINT_SECONDS:
        return ""
    minutes = int(age_seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


# Self-eviction: when the plugin is uninstalled/disabled but its files still
# linger in the version cache (Claude Code does not remove the statusLine key we
# wrote into ~/.claude/settings.json, and may keep the cached script on disk),
# this renderer would otherwise keep drawing a status line for a plugin that is
# no longer active. On each run we check whether the plugin is still enabled in
# any settings scope; if not, we remove our own statusLine entry and render
# nothing. SessionStart re-adds it whenever the plugin is genuinely active, so a
# transient mismatch self-heals on the next launch.
#
# The enabledPlugins key is ``<plugin>@<marketplace>``, and the marketplace half
# depends on where the user installed from: ``cognee-memory@cognee`` from this
# repo's own marketplace, ``cognee-memory@anthropic-plugin-directory`` from the
# official directory. Matching the exact id of one marketplace made the renderer
# evict its own bar for every install from the other, so we match on the plugin
# name and accept any marketplace.
_PLUGIN_NAME = "cognee-memory"
_PLUGIN_KEY_PREFIX = _PLUGIN_NAME + "@"
_USER_SETTINGS = Path.home() / ".claude" / "settings.json"
# A statusLine we consider "ours" to evict — never touch a user's own line.
_OWNED_STATUSLINE_MARKER = "cognee-statusline"


_SESSIONS_DIR = _SHARED_ROOT / "claude-code" / "sessions"


def _launch_record(session_id: str) -> dict:
    """This launch's record (``sessions/<host id>.json``), or {}.

    The host session id in the statusline context is the same key SessionStart
    files the record under, so the bar reads the dataset the launch is actually
    writing to — including one chosen with ``/cognee-memory:cognee-switch-datasets``.
    """
    if not _path_safe(session_id):
        return {}
    return _read_json(_SESSIONS_DIR / f"{session_id}.json")


def _active_dataset(session_id: str = "") -> str:
    # 1. the launch record (authoritative once SessionStart has run; switchable)
    recorded = str(_launch_record(session_id).get("dataset") or "").strip()
    if recorded:
        return recorded
    # 2. env var (inherited from the shell that launched Claude Code)
    v = os.environ.get("COGNEE_PLUGIN_DATASET", "").strip()
    if v:
        return v
    # 3. default
    return _DEFAULT_DATASET


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

# A failure verdict is only worth a red ✕ while it is FRESH. The hooks refresh a
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
_MISSING_URL_REASON = "missing_cognee_base_url"
# Local mode with no Python the Cognee server can run on. SessionStart writes the
# marker when it finds no 3.10+ interpreter (the plugin does not download one) and
# clears it once a runtime venv exists; like the missing-URL case it is proven
# from local state, not inferred from a failed connection — so it is shown at
# once, and named for the fix rather than the symptom (``unreachable``).
_PYTHON_REQUIRED_REASON = "cognee_needs_python_3_10"
_HOST_PYTHON_MARKER = _SHARED_ROOT / "host-python-unsupported.json"
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


def _breaker_glyph(active_url: str) -> str:
    """Red ✕ when THIS server's breaker is open, labeled with the real trip reason.

    The breaker file is keyed by base_url (SDK-356): an entry for a different
    server — a cloud tenant while this terminal is local, or Codex's target —
    must not red this bar. A legacy flat file (machine-wide, target-blind) is
    ignored for the same reason. The reason travels from the trip site, so a
    breaker opened by 5xx reads ``server_error``, not a false ``unreachable``.
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
    return _fail_glyph(_REASON_LABELS.get(reason, reason))


def _health_prefix(session_id: str = "") -> str:
    """Server-connection glyph for THIS session, from local markers (no network).

    Precedence — red is reserved for CONFIRMED, FRESH, DEFINITIVE failures:
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
        # Legacy marker (no 'state', has ready_at) reads as ready.
        state = str(marker.get("state") or ("ready" if marker.get("ready_at") else ""))
        if state in _FAIL_STATES:
            if time.time() - _checked_at(marker) <= _FAIL_STATE_STALE_SECONDS:
                return _fail_glyph(_REASON_LABELS.get(state, state))
            # Stale verdict: nobody has re-confirmed the failure — treat as
            # unknown rather than keep accusing a server that may be fine.
            return _breaker_glyph(active_url)
        if state == "ready":
            # Breaker override: recall is failing repeatedly even if the last
            # readiness write still says ready — that means we now know it's red.
            return _breaker_glyph(active_url) or _ok_glyph()

    # No usable marker: fall back to the breaker as the only failure signal.
    return _breaker_glyph(active_url)


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
    # Mid-session update guard: `/plugin update` installs the new version into
    # its own version-pinned cache dir, but settings.json keeps pointing the
    # status line at THIS (old) copy until the next SessionStart — so the
    # running-version check above never trips in the session where the update
    # happened. Claude Code does rewrite installed_plugins.json immediately,
    # so a recorded install at or past `latest` means the user already
    # updated: clear the nudge on this refresh instead of after a restart.
    recorded = _parse_semver(_recorded_install_version())
    published = _parse_semver(latest)
    if recorded and published and recorded >= published:
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


def _parse_semver(value: str):
    """Numeric X.Y.Z core as a tuple (ignoring -pre/+build), or None.

    Kept in sync with ``_plugin_common._parse_semver`` — duplicated because the
    renderer never imports the plugin runtime (see module docstring).
    """
    core = str(value or "").strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _recorded_install_version() -> str:
    """Newest cognee-memory version recorded in Claude Code's install registry.

    ``installed_plugins.json`` maps ``<plugin>@<marketplace>`` to a list of
    installs; it is rewritten the moment an update lands, regardless of which
    (possibly older) plugin copy this renderer belongs to. '' when the file is
    missing, unreadable, or has no parseable cognee-memory entry.
    """
    try:
        data = json.loads(_INSTALLED_PLUGINS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return ""
    best, best_str = (), ""
    for key, installs in plugins.items():
        if str(key).split("@", 1)[0] != _PLUGIN_NAME:
            continue
        for install in installs if isinstance(installs, list) else []:
            version = str(install.get("version") or "") if isinstance(install, dict) else ""
            parsed = _parse_semver(version)
            if parsed and parsed > best:
                best, best_str = parsed, version
    return best_str


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _enabled_in(path: Path):
    """Tri-state: True if any ``cognee-memory@<marketplace>`` key in this
    settings file is truthy, False if such keys exist but are all falsy, else
    None when no such key is present (file missing or no such entry)."""
    enabled = _read_json(path).get("enabledPlugins")
    if not isinstance(enabled, dict):
        return None
    matches = [v for k, v in enabled.items() if str(k).startswith(_PLUGIN_KEY_PREFIX)]
    if not matches:
        return None
    return any(bool(v) for v in matches)


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
        from _auto_memory import restore as restore_auto_memory

        restore_auto_memory()
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
        # The watcher may name a more specific cause (e.g. ``claude_not_logged_in``
        # when the LLM is the Claude observer and there is no key to be incorrect).
        reason = str(marker.get("reason") or "").strip()
        if not reason or not reason.replace("_", "").isalnum():
            reason = _LLM_KEY_REASON
        return _fail_glyph(reason)
    return ""


def _recall_marker(session_id: str) -> dict:
    """This session's recall marker, or {} when nothing attributable exists.

    Per session: ``recall/<session_key>.json`` is this session's own copy, so with
    several terminals open each bar shows its own numbers. The machine-wide
    ``last_recall.json`` (written for ``cognee_plugin.py``) is the fallback for hooks
    that predate the per-session copy, and is only trusted when it is unattributed or
    stamped with our session — a neighbour's counts must never appear here. The
    fallback carries no ``session_totals``: a cumulative number is only meaningful
    from the session that accumulated it.
    """
    marker = _read_json(_RECALL_DIR / f"{session_id}.json") if _path_safe(session_id) else {}
    if isinstance(marker.get("hits"), dict):
        return marker
    marker = _read_json(_RECALL_PATH)
    marked_key = str(marker.get("session_key") or "")
    if session_id and marked_key and session_id != marked_key:
        return {}
    marker.pop("session_totals", None)
    return marker


def _int(mapping, key) -> int:
    try:
        return int(mapping.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _recall_segment(session_id: str) -> str:
    """What memory did — this turn and over the session — or '' when unknown.

    ``session-context-lookup`` writes the recall marker on every prompt (hits per
    scope, what the previous turn persisted, and a per-session running total)
    precisely so the status line can show it without any network call.

    Default rendering, per turn at normal weight and the session total faint::

        · 5 memory hits · 12/40 turns had hits this session

    A session that has not had a single hit yet says so instead of showing a
    bare ``0/7`` (the graph is usually still filling up)::

        · 0 memory hits · memory warming up (7 turns)

    The per-turn number is the sum over every scope that returned something and
    was injected (session turns, traces, graph context, agent guidance, code).
    The per-scope diagnostic strip ``recall 4s/5t/0g/1a · saved 2p/41t/2a`` is
    still available with ``COGNEE_STATUSLINE_COUNTS=full``; ``false`` hides the
    segment entirely.
    """
    mode = os.environ.get("COGNEE_STATUSLINE_COUNTS", "").strip().lower()
    if mode in ("0", "false", "no", "off"):
        return ""
    marker = _recall_marker(session_id)
    hits = marker.get("hits")
    if not isinstance(hits, dict):
        return ""
    if mode == "full":
        return _recall_diagnostic_segment(marker, hits)

    total = sum(_int(hits, key) for key in hits)
    out = f" · {_plural(total, 'memory hit')}"
    totals = marker.get("session_totals")
    if isinstance(totals, dict):
        turns = _int(totals, "turns")
        with_hits = _int(totals, "turns_with_hits")
        if turns > 0:
            if with_hits > 0:
                cumulative = f"{with_hits}/{turns} turns had hits this session"
            else:
                cumulative = f"memory warming up ({_plural(turns, 'turn')})"
            out += f" \033[2m· {cumulative}\033[0m"
    return out


def _recall_diagnostic_segment(marker: dict, hits: dict) -> str:
    """The per-scope strip (``COGNEE_STATUSLINE_COUNTS=full``): hits per scope and
    what the previous turn persisted, faint so it sits below the health glyph and
    dataset in the visual hierarchy; the reset prevents color bleed."""
    recall = (
        f"{_int(hits, 'session')}s/{_int(hits, 'trace')}t/"
        f"{_int(hits, 'graph_context')}g/{_int(hits, 'session_context')}a"
    )
    saves = marker.get("saves_last_turn")
    if isinstance(saves, dict):
        saved = f"{_int(saves, 'prompt')}p/{_int(saves, 'trace')}t/{_int(saves, 'answer')}a"
        return f" \033[2m· recall {recall} · saved {saved}\033[0m"
    return f" \033[2m· recall {recall}\033[0m"


def _credits_segment() -> str:
    """Cloud credits balance + approximate cost of the last memory operation.

    Pure-local like everything here: reads only ``credits.json``, which the
    hooks write around each billable operation (see
    ``_plugin_common.refresh_credits``). Renders nothing unless ALL of: cloud
    mode, marker present with a numeric balance, marker younger than
    ``_CREDITS_MAX_AGE_SECONDS``, marker written for the server this session
    talks to, and not opted out. A reading older than
    ``_CREDITS_AGE_HINT_SECONDS`` renders with a faint age hint. Balance is green —
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
        remaining = None
    age = 0.0
    if remaining is not None:
        try:
            age = time.time() - float(entry.get("checked_at", 0) or 0)
        except (TypeError, ValueError):
            remaining = None
        if age > _CREDITS_MAX_AGE_SECONDS:
            remaining = None
    refused_op = _payment_required_op(entry)
    if remaining is None:
        if not refused_op:
            return ""
        # The server refused to pay for an operation but there is no balance to
        # show (the billing fetch itself failed): the refusal is the segment, and
        # the way out is the same as for a low balance — top up.
        return f" · \033[31mcredits: not enough for {refused_op}\033[0m{_top_up_hint()}"
    # Red from a dollar down: a balance about to run out is exactly the state the
    # user most needs to see, and the cloud refuses requests before it reaches
    # zero. Above that the number stays green even when the server refused an
    # operation — the refusal is the red part, not the balance.
    exhausted = remaining <= _CREDITS_LOW_USD
    color = "\033[31m" if exhausted else "\033[32m"
    sign = "-" if remaining < 0 else ""
    seg = f" · {color}credits: {sign}${abs(remaining):,.2f}\033[0m"
    if refused_op and not exhausted:
        seg += f" \033[31m(not enough for {refused_op})\033[0m"
    last_op = entry.get("last_op")
    if isinstance(last_op, dict):
        label = str(last_op.get("label") or "").strip()
        cost = last_op.get("cost_usd")
        if label and isinstance(cost, (int, float)) and not isinstance(cost, bool):
            # Normal weight (like the `cognee: <dataset>` text), NOT faint like
            # the recall/saved counters: what the last operation cost is a
            # first-class signal, not diagnostics.
            seg += f" · last {label} ~${cost:,.2f}"
    hint = _credits_age_hint(age)
    if hint:
        seg += f" \033[2m({hint})\033[0m"
    if exhausted:
        seg += _top_up_hint()
    return seg


def _top_up_hint() -> str:
    """Green so it reads as the way out, next to the red (low) balance. A plain URL:
    terminals linkify it themselves, and Claude Code's status line is not
    known to pass OSC 8 hyperlink escapes through."""
    return f" · \033[32mtop up: {_billing_url()}\033[0m"


def _forced_cloud_unconfigured() -> bool:
    """Forced cloud (backend switch) with no URL anywhere: nothing to connect
    to — a definitive misconfiguration this renderer can see directly from
    the environment, without waiting for a hook to record a failed attempt.
    """
    if forced_backend() != "cloud":
        return False
    return not os.environ.get("COGNEE_BASE_URL", "").strip()


def _runtime_python_missing() -> bool:
    """Local mode, and SessionStart recorded that no Python 3.10+ exists for the
    Cognee server. Marker-only (no probing): the hook that tried is the authority."""
    if _active_mode() != "local":
        return False
    try:
        return _HOST_PYTHON_MARKER.is_file()
    except OSError:
        return False


def _items_segment(session_id: str) -> str:
    from _session_stats import read as read_stats

    count = read_stats(session_id).get("entries_injected", 0)
    return f"{count} recalled · " if count else ""


def _status_prefix(session_id: str = "") -> str:
    """The single left glyph slot shared by the server- and LLM-key signals.

    One slot, by precedence — showing a green ● next to an ✕ would read as
    contradictory:
      0. forced cloud with no URL configured: a misconfiguration this renderer
         can prove on its own — the precise reason beats any marker-derived one
         (likewise local mode with no Python 3.10+ for the Cognee server,
         recorded by SessionStart: ``cognee_needs_python_3_10``)
      1. a server-connection failure wins: if we can't reach or authenticate
         against the server, its LLM key is not the actionable problem
      2. otherwise an LLM-key failure, which *replaces* the green ● (the
         ``llm_*`` reason already says the server side itself is fine)
      3. otherwise whatever the server signal is (``● `` or nothing).
    """
    if _forced_cloud_unconfigured():
        return _fail_glyph(_MISSING_URL_REASON)
    if _runtime_python_missing():
        return _fail_glyph(_PYTHON_REQUIRED_REASON)
    server = _health_prefix(session_id)
    # Membership, not startswith: the glyph is now preceded by its colour escape.
    if "✕" in server:
        return server
    return _llm_prefix(session_id) or server


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANIMATION_SECONDS = 10


def _classic_line(session_id: str) -> str:
    items = (
        _items_segment(session_id)
        if _path_safe(session_id) and session_id not in {".", ".."}
        else ""
    )
    return (
        f"{_status_prefix(session_id)}{items}"
        f"cognee: {_active_dataset(session_id)} · {_mode_label()}"
        f"{_credits_segment()}{_recall_segment(session_id)}{_update_segment()}"
    )


def _purple() -> str:
    return (
        "\033[38;5;93m"
        if os.environ.get("COGNEE_STATUSLINE_THEME", "").lower() == "light"
        else "\033[38;5;141m"
    )


def _unicode_output() -> bool:
    encoding = str(getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    return encoding in {"utf8", "utf_8"}


def _clean_label(value: str) -> str:
    return "".join(
        c if not unicodedata.category(c).startswith("C") else " " for c in _ANSI.sub("", value)
    )


def _ascii_line(value: str) -> str:
    replacements = {
        "■": "#",
        "●": "#",
        "✕": "[x]",
        "·": "|",
        "⬆": "^",
        "…": "...",
        "▰": "#",
        "▱": "-",
        "▔": "-",
        "▀": "#",
        "▄": "#",
    }
    for char in "▁▂▃▄▅▆▇█":
        replacements[char] = "#"
    return "".join(replacements.get(c, c if ord(c) < 128 else "?") for c in value)


def _width(value: str) -> int:
    return sum(
        0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1
        for c in _ANSI.sub("", value)
    )


def _columns() -> int:
    try:
        return max(1, min(120, int(os.environ.get("COLUMNS", "80"))))
    except (TypeError, ValueError):
        return 80


def _clip(value: str, width: int) -> str:
    """Last resort for tiny terminals; keep ANSI sequences intact and reset colour."""
    output, used, offset = [], 0, 0
    while offset < len(value):
        ansi = _ANSI.match(value, offset)
        if ansi:
            output.append(ansi.group())
            offset = ansi.end()
            continue
        char = value[offset]
        size = _width(char)
        if used + size > width:
            break
        output.append(char)
        used += size
        offset += 1
    return "".join(output) + _RESET


def _timestamp(value) -> float:
    try:
        if isinstance(value, (int, float)):
            return float(value)
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _hit_count(hits) -> int:
    return sum(max(0, _int(hits, key)) for key in hits) if isinstance(hits, dict) else 0


def _animation_phase(marker: dict, now: float) -> int | None:
    age = now - _timestamp(marker.get("ts"))
    return (
        int(age // 2) if _hit_count(marker.get("hits")) and 0 <= age < _ANIMATION_SECONDS else None
    )


def _ratio_bar(turns: int, with_hits: int, cells: int = 5) -> str:
    filled = round(max(0, min(with_hits, turns)) / turns * cells) if turns > 0 else 0
    return "▰" * filled + "▱" * (cells - filled)


def _tokens_saved(stats: dict, *, compact: bool = False) -> str:
    if _int(stats, "recalls") <= 0 or "chars_injected" not in stats:
        return "saved n/a" if compact else "tokens saved n/a"
    # Average context tokens per recall * recall count * 6.9 simplifies to
    # total context tokens * 6.9. Estimate tokens at four characters each;
    # multiply before rounding so small recalls retain their contribution.
    tokens = max(0, _int(stats, "chars_injected")) * 69 // 40
    if tokens >= 1_000_000:
        amount = f"{tokens // 100_000 / 10:g}m"
    elif tokens >= 1000:
        amount = f"{tokens // 100 / 10:g}k"
    else:
        amount = str(tokens)
    return f"~{amount}t saved" if compact else f"~{amount} tokens saved"


def _tetris_line(session_id: str, *, now: float | None = None) -> str:
    purple = _purple()
    now = time.time() if now is None else now
    unicode = _unicode_output()
    columns = _columns()
    marker = _recall_marker(session_id)
    phase = _animation_phase(marker, now)
    health = _status_prefix(session_id)
    # Branding is constant; failures and unknown health remain separate indicators.
    health = "" if "●" in health else health or "? "
    dataset = _clean_label(_active_dataset(session_id))
    color = "93" if os.environ.get("COGNEE_STATUSLINE_THEME", "").lower() == "light" else "141"
    badge = f"{purple}■\033[48;5;{color}m\033[30m cognee {_RESET}{purple}■{_RESET}"
    brand = f"{badge} {purple}\033[1m{dataset}{_RESET} · {purple}\033[1m{_active_mode()}{_RESET}"
    stats = (
        _read_json(_SESSIONS_DIR / session_id / "stats.json")
        if _path_safe(session_id) and session_id not in {".", ".."}
        else {}
    )
    count = max(0, _int(stats, "entries_injected"))
    shimmer = purple
    if phase is not None and phase % 2 == 0:
        shimmer = (
            "\033[38;2;208;180;255m"
            if os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}
            else "\033[38;5;183m"
        )
    chip = f"{shimmer}{count} reminiscence{_RESET}"
    core = health + brand + " · " + chip
    counts_mode = os.environ.get("COGNEE_STATUSLINE_COUNTS", "").strip().lower()
    counts, decoration = "", ""
    if counts_mode not in {"0", "false", "no", "off"} and isinstance(marker.get("hits"), dict):
        if counts_mode == "full":
            counts = _recall_diagnostic_segment(marker, marker["hits"])
        else:
            counts = f" · {_plural(_hit_count(marker['hits']), 'idea')}"
            totals = marker.get("session_totals")
            if isinstance(totals, dict):
                turns = max(0, _int(totals, "turns"))
                with_hits = max(0, min(turns, _int(totals, "turns_with_hits")))
                counts += f" · {with_hits}/{turns} imprints"
                decoration = _ratio_bar(turns, with_hits)
            if decoration:
                decoration = f"  {purple}{decoration}{_RESET}"
    from _quality_summary import segment as quality_segment

    quality_args = (_SESSIONS_DIR.parent, os.environ.get("COGNEE_BASE_URL", "").rstrip("/"))
    quality = quality_segment(*quality_args, now=now, bar=columns > 60)
    compact_quality = quality_segment(*quality_args, now=now, compact=True, bar=columns > 60)
    quality = purple + quality + _RESET
    compact_quality = purple + compact_quality + _RESET
    credits, update = _credits_segment(), _update_segment()
    savings = " · " + _tokens_saved(stats)
    short_savings = " · " + _tokens_saved(stats, compact=True)
    # Exactly one bar: quality replaces the imprint ratio decoration. Keep the
    # numeric imprint count where space permits; quality remains visible at 80.
    quality_core = health + brand
    imprint_counts = re.sub(r" · \d+ ideas?", "", counts)
    candidates = [
        core + counts + " · " + quality + savings + credits + update,
        core + counts + " · " + compact_quality + short_savings + credits + update,
        core + imprint_counts + " · " + compact_quality + short_savings + credits + update,
        core + " · " + compact_quality + short_savings + credits + update,
        quality_core + " · " + quality + short_savings + credits + update,
        quality_core + " · " + compact_quality + short_savings + credits + update,
        quality_core + " · " + compact_quality + short_savings,
        quality_core + " · " + compact_quality,
        health + badge + " · " + compact_quality,
    ]
    if columns > 60:
        candidates = [
            variant
            for candidate in candidates
            for variant in (candidate.replace(" · ", "  ·  "), candidate)
        ]
    if not unicode:
        candidates = [_ascii_line(candidate) for candidate in candidates]
    for candidate in candidates:
        if _width(candidate) <= columns:
            return candidate
    # Long dataset/reason or a very small terminal: preserve a single safe line.
    return _clip(candidates[-1], columns)


def main() -> None:
    classic = os.environ.get("COGNEE_STATUSLINE_STYLE", "").strip().lower() == "classic"
    # Preserve legacy UTF-8 output in classic mode. The default deliberately
    # honours non-UTF-8 stdout and uses ASCII instead of changing its encoding.
    for stream in (sys.stdin, sys.stdout) if classic else (sys.stdin,):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        ctx = json.load(sys.stdin)
    except Exception:
        ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}
    workspace = ctx.get("workspace") if isinstance(ctx.get("workspace"), dict) else {}
    cwd = str(ctx.get("cwd") or workspace.get("current_dir") or workspace.get("project_dir") or "")
    session_id = str(ctx.get("session_id") or "")
    if not _path_safe(session_id) or session_id in {".", ".."}:
        session_id = ""
    try:
        if not _plugin_enabled(cwd):
            _evict_own_statusline()
            return
        try:
            line = _classic_line(session_id) if classic else _tetris_line(session_id)
        except Exception:
            line = _classic_line(session_id)
        line = line.replace("\n", " ").replace("\r", " ")
        if not classic and not _unicode_output():
            line = _ascii_line(line)
        sys.stdout.write(line)
    except Exception:
        # Status must never interrupt the host, including partially written markers.
        return


if __name__ == "__main__":
    main()
