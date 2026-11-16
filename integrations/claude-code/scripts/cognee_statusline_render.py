#!/usr/bin/env python3
"""Render the Cognee status line.

Invoked by Claude Code's ``statusLine`` (via ``cognee-statusline.sh``), which
pipes a JSON context on stdin. Deliberately standalone and pure-local: reads
only env vars (``~/.cognee/.env`` included) and the plugin's own state files —
no network calls, no ``_plugin_common`` import.

Output: ``cognee: <dataset-name> · local`` or ``cognee: <dataset-name> · cloud``
(``auto (<dataset-name>)`` in place of the name while recall is auto)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

from _env_file import forced_backend, load_env_file
from _net import is_loopback_host

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
# Live activity (see _plugin_common.phase_begin): what memory is doing now and
# the last thing it finished, per host session.
_RECALL_PATH = _SHARED_ROOT / "claude-code" / "last_recall.json"
_RECALL_DIR = _SHARED_ROOT / "claude-code" / "recall"
_PHASE_DIR = _SHARED_ROOT / "claude-code" / "phase"
# Written by the plugin's hooks module for a session whose status row it draws
# above the prompt (the band); this renderer then prints nothing for that
# session, so the row is never shown twice. Keyed by host session id.
_BAND_DIR = _SHARED_ROOT / "claude-code" / "band"
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

# Disabled plugin: when the plugin is uninstalled/disabled but its files still
# linger in the version cache (Claude Code does not remove a statusLine key the
# user, or an earlier plugin version, wrote into ~/.claude/settings.json), this
# renderer would otherwise keep drawing a status line for a plugin that is no
# longer active. On each run we check whether the plugin is still enabled in
# any settings scope; if not, we render nothing. Settings are only read.
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
    if v and v.lower() != "auto":  # reserved: "auto" means the default dataset
        return v
    # 3. default
    return _DEFAULT_DATASET


def _recall_auto(host_id: str = "") -> bool:
    """Whether graph recall is auto for this launch, from local state only.

    Mirrors ``_plugin_common.recall_mode`` (this renderer does not import it): an
    explicit read set pins; the launch record's own ``recall_pinned`` wins either
    way, and a switch on a record written before recall modes existed pins;
    otherwise ``COGNEE_PLUGIN_RECALL_MODE=pinned`` pins. Anything else is auto.
    """
    if os.environ.get("COGNEE_PLUGIN_READ_DATASET_IDS", "").strip():
        return False
    rec = _launch_record(str(host_id or ""))
    if "recall_pinned" in rec:
        return rec.get("recall_pinned") is not True
    if rec.get("switched_at"):
        return False
    return os.environ.get("COGNEE_PLUGIN_RECALL_MODE", "").strip().lower() != "pinned"


def _dataset_label(host_id: str = "", recall_auto: bool = False) -> str:
    """The dataset as the status shows it: ``auto (<dataset>)`` while recall is
    auto (the server picks the readable datasets each recall searches, and saves
    go to ``<dataset>``), else the name, as Claude Code's row does."""
    dataset = _active_dataset(str(host_id or ""))
    return f"auto ({dataset})" if recall_auto else dataset


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
    host = urlparse(url).hostname or ""
    return "local" if not host or is_loopback_host(host) else "cloud"


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
    """No update segment: the plugin no longer checks for updates (SDK-1411).

    Claude Code's own plugin updates cover directory and marketplace installs; a
    marker an earlier version left behind is ignored.
    """
    return ""


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


def _llm_prefix(session_id: str = "") -> str:
    """Amber LLM failure glyph, or '' — local mode only, read from marker.

    The local server's LLM matters only in local mode, so this is suppressed in
    cloud mode. The verdict comes from the background idle watcher, which only
    knows whether an LLM is configured at all (SDK-1411): `not_set` = no LLM key
    or provider option. `auth_failed` is still read from markers the watcher of
    1.8.3 or older wrote about the Claude observer. Distinct reasons
    from the server-connection ones so the two aren't confused. Amber (`\\033[1;33m`)
    so a broken LLM key reads differently at a glance from an uncolored
    server-connection failure; the reset lands before the trailing space so no
    color bleeds into the rest of the bar.

    Per terminal, because the answer can differ per terminal: each launch decides
    from its own options whether an LLM is configured. We read THIS session's record
    (``llm-state/<session_key>.json``) first; the shared marker is consulted only
    when we have none of our own, and then only if it is unattributed — another
    terminal's "not_set" must not red a bar whose key is fine, and its "ok" must not
    green a bar whose key is missing.

    Verdicts expire (`_LLM_STATE_STALE_SECONDS`): the marker is shared by every
    session, so without a TTL one stale write could keep accusing a key that has
    since been fixed. The watcher renews its verdict at launch and every five
    minutes while it runs, so a live session stays well inside the window, and a
    verdict left by a session whose watcher is gone drops off after it.
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
        # A marker may name a more specific cause.
        reason = str(marker.get("reason") or "").strip()
        if not reason or not reason.replace("_", "").isalnum():
            reason = _LLM_KEY_REASON
        return _fail_glyph(reason)
    return ""


# An ``active`` phase older than this is a hook that died mid-flight (or a
# marker a previous session left); it must not keep the bar "recalling…" forever.
_PHASE_STALE_SECONDS = 90.0
# A finished result stays on the bar until the next phase begins, faint once it
# is this old so "recalled 3 memories" reads as recent versus left over.
_RESULT_DIM_AFTER_SECONDS = 60.0
# A result older than this is no longer news: the segment leaves the line.
_RESULT_HIDE_AFTER_SECONDS = 300.0
_ACTIVE_STYLE = "\033[33m"  # yellow: something is happening right now (local mode is cyan)
_DIM = "\033[2m"

# One user-facing phrase per phase, present tense while it runs. The vocabulary
# is deliberately plain: "memories" is the only noun, and the idle improve pass
# is "learning from this session", which is what it does.
_ACTIVE_LABELS = {
    "connecting": "connecting…",
    "setting_up": "setting up memory",
    "recalling": "recalling…",
    "saving": "saving…",
    "consolidating": "learning from this session…",
    "indexing": "indexing repository…",
    "anchoring": "preserving context…",
    "syncing": "saving session…",
}


def _band_draws_row(session_id: str) -> bool:
    """True when the hooks module draws this session's row above the prompt.

    Only this session's marker counts: a terminal without the module (an older
    Claude Code, ``--safe-mode``, a managed org) keeps its status line.
    """
    if not session_id or not _path_safe(session_id):
        return False
    return (_BAND_DIR / f"{session_id}.json").is_file()


def _phase_marker(session_id: str) -> dict:
    """This session's activity marker, or {} when there is none to attribute.

    Keyed by the host session id from stdin, so with several terminals open each
    bar shows what *its* session is doing. Never read another session's file.
    """
    if not session_id or not _path_safe(session_id):
        return {}
    return _read_json(_PHASE_DIR / f"{session_id}.json")


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    plural = word[:-1] + "ies" if word.endswith("y") else word + "s"
    return f"{n} {plural}"


def _active_label(active: dict) -> str:
    phase = str(active.get("phase") or "")
    label = _ACTIVE_LABELS.get(phase, "")
    detail = str(active.get("detail") or "").strip()
    if phase == "setting_up":
        return f"{label} · {detail}…" if detail else f"{label}…"
    return label


def _result_label(kind: str, entry: dict) -> str:
    """What an operation did, at rest: a number only for recall (what helped
    this turn), a ticked verb for everything that keeps work (a count of
    captured entries is bookkeeping nobody can act on)."""
    ok = entry.get("ok", True) is not False
    count = entry.get("count")
    try:
        count = int(count) if count is not None else None
    except (TypeError, ValueError):
        count = None
    if kind == "recalled":
        if not ok:
            return "recall failed"
        if not count:
            return "nothing relevant"
        return f"{_plural(count, 'memory')} used"
    if kind == "saved":
        if ok or count:
            return "✓ memory saved"
        # Nothing landed this turn: spooled for replay, or dropped for good.
        return "save queued" if entry.get("buffered") is True else "save failed"
    if kind == "ready":
        return "memory ready" if ok else "memory unavailable"
    if kind == "consolidated":
        return "✓ learned" if ok else "learning failed"
    if kind == "indexed":
        return "✓ repository indexed" if ok else "indexing failed"
    if kind == "anchored":
        return "✓ context kept" if ok else "context not kept"
    if kind == "synced":
        return "✓ session saved" if ok else "session sync failed"
    return ""


def _activity(session_id: str) -> dict:
    """What to show: ``{"kind": "active"|"result", "text": ..., "age": s}`` or {}.

    An active phase wins; otherwise the most recent result. A stale active phase
    (a hook that died) is ignored rather than shown forever. A failed ``ready``
    result is ignored once the connection marker says the server is up: the
    verdict predates the marker (a boot that finished after the hook gave up),
    and "memory unavailable" next to a green ● would contradict it.
    """
    marker = _phase_marker(session_id)
    if not marker:
        return {}
    now = time.time()
    active = marker.get("active")
    if isinstance(active, dict) and active.get("phase"):
        try:
            age = now - float(active.get("updated_at") or 0)
        except (TypeError, ValueError):
            age = _PHASE_STALE_SECONDS
        if age < _PHASE_STALE_SECONDS:
            text = _active_label(active)
            if text:
                return {
                    "kind": "active",
                    "phase": str(active.get("phase")),
                    "text": text,
                    "age": age,
                }
    results = marker.get("results")
    if not isinstance(results, dict):
        return {}
    finished: list[tuple[float, str, dict]] = []
    for kind, entry in results.items():
        if not isinstance(entry, dict):
            continue
        try:
            at = float(entry.get("at") or 0)
        except (TypeError, ValueError):
            continue
        finished.append((at, str(kind), entry))
    finished.sort(key=lambda item: item[0], reverse=True)
    latest_kind, latest, latest_at = "", {}, -1.0
    for at, kind, entry in finished:
        if kind == "ready" and entry.get("ok", True) is False and "●" in _health_prefix(session_id):
            continue
        latest_kind, latest, latest_at = kind, entry, at
        break
    text = _result_label(latest_kind, latest) if latest_kind else ""
    if not text:
        return {}
    return {
        "kind": "result",
        "phase": latest_kind,
        "text": text,
        "age": max(0.0, now - latest_at),
        "ok": latest.get("ok", True) is not False,
    }


def _activity_segment(session_id: str, style: str = "") -> str:
    """`` · recalling…`` while memory works, `` · 3 memories used`` after.

    Reads the per-session activity marker the hooks write (no network). Active
    phases render in the accent colour; a finished result renders plain, then
    faint once it is a minute old, and leaves after five.
    ``COGNEE_STATUSLINE_ACTIVITY=false`` hides it.
    """
    if os.environ.get("COGNEE_STATUSLINE_ACTIVITY", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return ""
    shown = _activity(session_id)
    if not shown:
        return ""
    if shown["kind"] == "active":
        return f" · {_ACTIVE_STYLE}{shown['text']}{_RESET}"
    age = shown.get("age", 0.0)
    if age >= _RESULT_HIDE_AFTER_SECONDS:
        return ""
    # ``style`` colours a finished result (the badge line passes its purple);
    # the classic line passes nothing and keeps its plain text.
    if age >= _RESULT_DIM_AFTER_SECONDS:
        return f" {_DIM}· {style}{shown['text']}{_RESET}"
    return f" · {style}{shown['text']}{_RESET}" if style else f" · {shown['text']}"


def _icon() -> str:
    """An optional brain in front of the name (``COGNEE_STATUSLINE_ICON=brain``).

    Off by default in the terminal: the emoji is double-width and some terminal
    fonts misrender it, and the green dot already carries "connected".
    """
    choice = os.environ.get("COGNEE_STATUSLINE_ICON", "").strip().lower()
    return "🧠 " if choice == "brain" else ""


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


def _classic_line(session_id: str) -> str:
    items = (
        _items_segment(session_id)
        if _path_safe(session_id) and session_id not in {".", ".."}
        else ""
    )
    return (
        f"{_status_prefix(session_id)}{items}{_icon()}"
        f"cognee: {_dataset_label(session_id, _recall_auto(session_id))} · {_mode_label()}"
        f"{_recall_segment(session_id)}{_activity_segment(session_id)}"
        f"{_update_segment()}"
    )


def _light_theme() -> bool:
    """COGNEE_STATUSLINE_THEME first; else Claude Code's own ``theme`` setting
    (``light``, ``light-daltonized``). ``auto`` cannot be told apart here."""
    forced = os.environ.get("COGNEE_STATUSLINE_THEME", "").lower()
    if forced in ("light", "dark"):
        return forced == "light"
    try:
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text("utf-8"))
        return str(settings.get("theme") or "").lower().startswith("light")
    except Exception:
        return False


def _purple() -> str:
    return "\033[38;5;93m" if _light_theme() else "\033[38;5;141m"


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
        "○": "o",
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


def _columns() -> int | None:
    """The terminal width when the host reports one; None means do not fit."""
    try:
        return max(1, int(os.environ["COLUMNS"]))
    except (KeyError, TypeError, ValueError):
        return None


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


def _tetris_line(session_id: str, *, now: float | None = None) -> str:
    del now  # kept for callers; the line no longer animates
    purple = _purple()
    unicode = _unicode_output()
    columns = _columns()
    # The same row the mod draws above the prompt, so the two never look like
    # different things: the connection glyph (a green ● once the server is
    # confirmed up and authenticated, a dim ○ while nothing is known yet, the
    # red ✕ and its reason on a failure), the wordmark in the brand purple, the
    # dataset plain, the mode in purple.
    health = _status_prefix(session_id)
    if "●" in health:
        lead, health = f"{_OK_STYLE}●{_RESET} ", ""
    elif health:
        lead, health = health.rstrip() + " ", ""
    else:
        lead = f"{_DIM}○{_RESET} "
    dataset = _clean_label(_dataset_label(session_id, _recall_auto(session_id)))
    badge = f"{lead}{purple}\033[1mcognee{_RESET}"
    brand = f"{badge} {dataset} · {purple}\033[1m{_active_mode()}{_RESET}"
    core = health + brand
    # No counters: the activity segment says what the last recall did. (The
    # classic line keeps its own.)
    update = _update_segment()
    # What memory is doing right now, in the brand purple at rest (an active
    # phase keeps its accent colour).
    emphasis = f"{purple}\033[1m"
    activity = _activity_segment(session_id, style=emphasis)
    # No reported width (the normal Claude Code case) shows the full line.
    wide = columns is None or columns > 60
    # Keep the badge at every width: compact the spacing before dropping the
    # activity.
    candidates = [
        core + activity + update,
        core.replace(" · ", "·") + activity + update,
    ]
    # An update notice is never dropped to save width: it is the segment a user
    # has to act on.
    if not update:
        candidates += [core, core.replace(" · ", "·")]
    else:
        candidates += [
            core.replace(" · ", "·") + update,
            badge + " " + update.lstrip(" ·"),
            # Narrowest form with a notice: health plus the notice alone.
            health + update.lstrip(" ·"),
        ]
    if wide:
        # Prefer breathing room, then compact spacing before dropping data.
        candidates = [
            variant
            for candidate in candidates
            for variant in (candidate.replace(" · ", "  ·  "), candidate)
        ]
    if not unicode:
        candidates = [_ascii_line(candidate) for candidate in candidates]
    # Claude Code does not normally export COLUMNS to the status line command;
    # without a real width, show the full line as classic does.
    if columns is None:
        return candidates[0]
    for candidate in candidates:
        if _width(candidate) <= columns:
            return candidate
    # Long dataset/reason or a very small terminal: preserve a single safe line.
    return _clip(candidates[-1], columns)


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


def _snapshot(session_id: str, enabled: bool) -> dict:
    """The bar as structured data (``--json``), for the Claude Code mod.

    The mod draws the same row on the desktop band, where the settings-file
    status line does not render, so one renderer feeds both surfaces and the two
    never drift. Same inputs as the text bar, no network, no eviction.
    """
    prefix = _plain(_status_prefix(session_id)).strip()
    if "✕" in prefix:
        state, reason = "fail", prefix[prefix.find("(") + 1 : prefix.rfind(")")]
    elif "●" in prefix:
        state, reason = "ok", ""
    else:
        state, reason = "", ""
    activity = _activity(session_id)
    update = _plain(_update_segment()).strip(" ·")
    dataset = _active_dataset(session_id)
    mode = _active_mode()
    line = f"cognee: {dataset} · {mode}"
    for segment in (activity.get("text", ""), update):
        if segment:
            line += f" · {segment}"
    return {
        "enabled": enabled,
        "state": state,
        "reason": reason,
        "dataset": dataset,
        # Local state only, like the rest: the band labels the dataset
        # "auto (<dataset>)" from its first draw, before the server's listing.
        "recall": "auto" if _recall_auto(session_id) else "pinned",
        "mode": mode,
        "activity": activity,
        "update": update,
        "text": line,
    }


def main() -> None:
    as_json = "--json" in sys.argv[1:]
    classic = os.environ.get("COGNEE_STATUSLINE_STYLE", "").strip().lower() == "classic"
    # UTF-8 in both styles, as before 1.6.22 (Windows pipes default to cp1252,
    # which cannot encode the status glyphs). The default style falls back to
    # ASCII only if stdout cannot be switched.
    for stream in (sys.stdin, sys.stdout):
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
    # Host session id: markers are per-integration, not per-session, so both the
    # LLM-key verdict and the activity are attributed before being shown.
    session_id = str(ctx.get("session_id") or "")
    if not _path_safe(session_id) or session_id in {".", ".."}:
        session_id = ""
    try:
        if not _plugin_enabled(cwd):
            if as_json:
                # The mod asks on behalf of a session that IS running the plugin;
                # report rather than evict (the settings entry is the bar's, not ours).
                sys.stdout.write(json.dumps(_snapshot(session_id, enabled=False)))
                return
            # Plugin uninstalled/disabled but files linger: render nothing. The
            # settings entry is the user's to remove; the plugin never writes
            # Claude Code's settings (SDK-1411).
            return
        if as_json:
            sys.stdout.write(json.dumps(_snapshot(session_id, enabled=True)))
            return
        if _band_draws_row(session_id):
            # The hooks module draws this session's row above the prompt: stay quiet.
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
