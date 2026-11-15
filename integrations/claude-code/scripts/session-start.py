#!/usr/bin/env python3
"""Initialize Cognee memory at session start.

Runs on the SessionStart hook. Responsibilities:
  1. Load config (file + env vars)
  2. Compute per-directory session ID
  3. Connect to Cognee Cloud if configured
  4. Configure local LLM if local mode
  5. Register the current Claude session as an active agent connection
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from _net import is_loopback_host, open_url

# Add scripts dir to path for config import
sys.path.insert(0, os.path.dirname(__file__))
from _logfiles import console_capture_tail as _console_capture_tail
from _logfiles import rotate_if_oversized as _rotate_log_if_oversized
from _logfiles import start_console_capture as _start_console_capture
from _plugin_common import (
    _COGNEE_CACHE_DIR,
    _COGNEE_DATA_DIR,
    _COGNEE_HOME,
    _COGNEE_SYSTEM_DIR,
    _VENV_DIR,
    _VENV_PYTHON,
    _VENV_READY_MARKER,
    PRESENCE_ABSENT,
    PRESENCE_READY,
    _api_key_with_source,
    _https_context,
    _is_loopback_host,
    _reexec_into_venv,
    apply_cognee_env,
    clear_server_pidfile,
    ensure_launch_record,
    get_session_key,
    hook_log,
    is_observer_child,
    managed_endpoint_enabled,
    phase_begin,
    phase_end,
    phase_touch,
    probe_health,
    quiet_hook_output,
    resolve_session_key_from_payload,
    server_presence,
    server_ready_hint,
    service_url_is_local,
    set_launch_base_url,
    set_session_key,
    touch_activity,
    write_connection_state,
    write_server_pidfile,
)
from _proc import find_host_ancestor_windows
from _proc import pid_alive as _pid_alive
from config import (
    _cloud_http_request,
    _user_id_via_api,
    coding_memory_enabled,
    ensure_cognee_ready,
    ensure_dataset_ready_via_api,
    get_dataset,
    is_cloud_mode,
    load_config,
)

_STATE_DIR = Path.home() / ".cognee-plugin" / "claude-code"
_GLOBAL_STATE_DIR = Path.home() / ".cognee-plugin"
_WATCHER_PID = _STATE_DIR / "watcher.pid"
_WATCHER_STOP = _STATE_DIR / "watcher.stop"
_WATCHER_SCRIPT = Path(__file__).with_name("idle-watcher.py")
_EXIT_WATCHER_SCRIPT = Path(__file__).with_name("exit-watcher.py")
_EXIT_WATCHERS_DIR = _STATE_DIR / "exit-watchers"
_LOCAL_SERVICE_URL = "http://localhost:8011"
_HEALTH_URL = f"{_LOCAL_SERVICE_URL}/health"
_HEALTH_TIMEOUT_SECONDS = 30
_HEALTH_POLL_SECONDS = 1.0
_SERVER_BOOT_LOCK = _GLOBAL_STATE_DIR / "server-bootstrap.lock"
_SERVER_BOOT_LOCK_STALE_SECONDS = 2 * _HEALTH_TIMEOUT_SECONDS
_SERVER_BOOT_LOCK_WAIT_SECONDS = _HEALTH_TIMEOUT_SECONDS + 5.0
_SERVER_BOOT_LOCK_POLL_SECONDS = 0.2

# Lazy bootstrap: defer server boot + registration to a detached worker so the
# SessionStart hook returns fast and migrations never time out the 15s budget.
_BOOTSTRAP_ARG = "--bootstrap"
_BOOTSTRAP_LOCK = _GLOBAL_STATE_DIR / "server-bootstrap-worker.lock"
_SERVER_BOOT_DEADLINE_SECONDS = float(os.environ.get("COGNEE_SERVER_BOOT_DEADLINE", "") or 600.0)
_BOOTSTRAP_LOCK_STALE_SECONDS = _SERVER_BOOT_DEADLINE_SECONDS + 60.0
# How often the bootstrap worker refreshes the ``setting_up`` phase while it
# boots: well inside the 90 s after which the status surfaces call an active
# phase dead (``_plugin_common._PHASE_STALE_SECONDS``).
_SETTING_UP_HEARTBEAT_SECONDS = 15.0
_LAZY_BOOTSTRAP = os.environ.get("COGNEE_LAZY_BOOTSTRAP", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

# Written by earlier versions when they could not build the runtime venv; read
# (and cleared once a venv is ready) so a stale refusal still reaches the user.
_HOST_PYTHON_MARKER = _GLOBAL_STATE_DIR / "host-python-unsupported.json"
# Why local mode cannot start: no user-built venv, cognee too old, or missing
# driver extras (the plugin installs nothing, SDK-1411). Shown at SessionStart.
_COGNEE_INSTALL_MARKER = _GLOBAL_STATE_DIR / "cognee-install-needed.json"
_PINNED_COGNEE_VERSION = "1.6.6"
# Maps a provider setting to the cognee package "extra" that provides its
# driver. A bare `cognee==<version>` install has none, so the server would fail
# on first use. Only `fastembed` embeddings (also what the Claude observer
# applies) are left: the plugin's own server always runs on Cognee's built-in
# stores (SQLite, LanceDB, Kuzu) with an allowlisted environment (see
# _server_env), so a database provider in the shell never reaches it and must
# not block the start for a driver it would not use; a local Ollama LLM is not
# supported in this version (no endpoint option). LLM providers whose cognee
# adapter imports an SDK the base install lacks need their extra too:
# `anthropic` imports `anthropic`, `mistral` imports `mistralai` (openai and
# gemini run on litellm, which the base install has). The values sit in
# os.environ by now: both providers come from the plugin options
# (_env_file.py), applied at process start.
_PROVIDER_EXTRAS = (
    ("EMBEDDING_PROVIDER", {"fastembed": "fastembed"}),
    ("LLM_PROVIDER", {"anthropic": "anthropic", "mistral": "mistral"}),
)


def _detect_required_extras() -> list:
    """Detect the cognee extras actually needed from the configured backend
    providers, so the install below brings in exactly what's needed instead of
    either a bare install (missing drivers, #232) or unconditionally installing
    every possible extra regardless of what's actually configured."""
    extras: list[str] = []
    for env_var, mapping in _PROVIDER_EXTRAS:
        value = os.environ.get(env_var, "").strip().lower()
        extra = mapping.get(value)
        if extra and extra not in extras:
            extras.append(extra)
    return extras


def _cognee_install_spec() -> str:
    """The pip requirement to install: the pinned cognee, plus the extras the
    configured providers need."""
    extras = ",".join(_detect_required_extras())
    return (
        f"cognee[{extras}]=={_PINNED_COGNEE_VERSION}"
        if extras
        else f"cognee=={_PINNED_COGNEE_VERSION}"
    )


def _apply_key_limit_warning(output: dict) -> dict:
    """Warn (systemMessage) while this machine runs on the personal key because
    the server refused it a key of its own: the plugin's agent is full (409).

    Read from a marker so a detached bootstrap's verdict reaches the user too —
    on the next launch — and repeated every launch until a key is provisioned.
    """
    from _plugin_common import key_limit_message

    message = key_limit_message()
    if not message:
        return output
    result = dict(output or {})
    hso = dict(result.get("hookSpecificOutput") or {})
    hso.setdefault("hookEventName", "SessionStart")
    existing = str(hso.get("systemMessage") or "").strip()
    hso["systemMessage"] = f"{existing}\n\n{message}" if existing else message
    result["hookSpecificOutput"] = hso
    top = str(result.get("systemMessage") or "").strip()
    result["systemMessage"] = f"{top}\n\n{message}" if top else message
    return result


def _apply_host_python_warning(output: dict) -> dict:
    """Append the recorded fallback refusal (if any) to the hook's systemMessage.

    The marker outlives the session that wrote it on purpose: every launch on
    the broken host repeats the message until a venv is built (which clears it
    in ``_write_venv_ready``) — this is the loud failure SDK-617 asks for.
    """
    messages = []
    for marker in (_HOST_PYTHON_MARKER, _COGNEE_INSTALL_MARKER):
        try:
            if marker.exists():
                text = str(json.loads(marker.read_text(encoding="utf-8")).get("message") or "")
                if text:
                    messages.append(text)
        except Exception as exc:
            hook_log("host_python_marker_read_failed", {"error": str(exc)[:200]})
    message = "\n\n".join(messages)
    if not message:
        return output
    result = dict(output or {})
    hso = dict(result.get("hookSpecificOutput") or {})
    hso.setdefault("hookEventName", "SessionStart")
    existing = str(hso.get("systemMessage") or "").strip()
    hso["systemMessage"] = "{}\n\n{}".format(existing, message) if existing else message
    result["hookSpecificOutput"] = hso
    # Also at the top level: that is where Claude Code documents the universal
    # ``systemMessage`` field, and where the Antigravity adapter reads it.
    top = str(result.get("systemMessage") or "").strip()
    result["systemMessage"] = "{}\n\n{}".format(top, message) if top else message
    return result


def _venv_cognee_version() -> str:
    """Installed cognee version inside the plugin venv, or '' if unimportable."""
    if not _VENV_PYTHON.exists():
        return ""
    try:
        out = subprocess.run(
            [
                str(_VENV_PYTHON),
                "-c",
                "import importlib.metadata as m; print(m.version('cognee'))",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception as exc:
        hook_log("cognee_version_probe_failed", {"error": str(exc)[:200]})
    return ""


# One distribution per extra whose presence in the venv proves that extra's
# drivers are installed (verified against cognee 1.6.6's optional-dependencies:
# fastembed and codegraph are empty extras there, their packages being core).
# Probing the venv is deliberately preferred over recording installed extras in
# venv-ready.json: that marker is shared with plugins that don't know about
# extras (codex/openclaw), whose rewrites would wipe the record and force a
# reinstall on every cold boot after theirs — the exact churn skip-at-pin
# exists to avoid.
_EXTRA_SENTINEL_DISTS = {
    "fastembed": "fastembed",
    "anthropic": "anthropic",
    "mistral": "mistralai",
}


def _venv_missing_extras(extras: list) -> list:
    """Which of the required extras' driver packages are absent from the venv.

    Ground truth for the skip-at-pin gate: the venv being at the pinned cognee
    version says nothing about extras — a provider configured AFTER the venv
    was built (or a bare install done by an extras-unaware plugin sharing this
    venv) leaves the pin satisfied but the drivers missing, and #232 comes
    right back. Fails soft (reports nothing missing, logged) on probe errors:
    a venv broken enough to fail this probe also fails the version probe, and
    the install path handles that.
    """
    if not extras:
        return []
    if not _VENV_PYTHON.exists():
        return list(extras)
    dists = [_EXTRA_SENTINEL_DISTS[extra] for extra in extras]
    try:
        out = subprocess.run(
            [
                str(_VENV_PYTHON),
                "-c",
                "import importlib.metadata as m, sys\n"
                "for d in sys.argv[1:]:\n"
                "    try:\n"
                "        m.version(d)\n"
                "    except Exception:\n"
                "        print(d)",
                *dists,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if out.returncode != 0:
            hook_log("extras_probe_failed", {"stderr": out.stderr[:200]})
            return []
        missing_dists = set(out.stdout.split())
        return [extra for extra, dist in zip(extras, dists) if dist in missing_dists]
    except Exception as exc:
        hook_log("extras_probe_failed", {"error": str(exc)[:200]})
        return []


def _write_venv_ready(version: str) -> None:
    try:
        payload = {
            "cognee_version": version,
            "python": str(_VENV_PYTHON),
            "updated_at": time.time(),
        }
        _VENV_READY_MARKER.parent.mkdir(parents=True, exist_ok=True)
        tmp = _VENV_READY_MARKER.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, _VENV_READY_MARKER)
    except Exception as exc:
        hook_log("venv_ready_write_failed", {"error": str(exc)[:200]})
    try:
        # A usable venv exists, so an earlier refusal (if any) is moot.
        _HOST_PYTHON_MARKER.unlink(missing_ok=True)
        _COGNEE_INSTALL_MARKER.unlink(missing_ok=True)
    except Exception as exc:
        hook_log("host_python_marker_unlink_failed", {"error": str(exc)[:200]})


def _version_tuple(version: str) -> tuple:
    parts = []
    for piece in str(version or "").split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def manual_install_command() -> str:
    """The one command that builds the local runtime the plugin no longer installs."""
    spec = _cognee_install_spec()
    if os.name == "nt":
        venv = r"%USERPROFILE%\.cognee-plugin\venv"
        return rf'py -3.12 -m venv "{venv}" && "{venv}\Scripts\python.exe" -m pip install "{spec}"'
    return (
        "python3 -m venv ~/.cognee-plugin/venv && "
        f'~/.cognee-plugin/venv/bin/python -m pip install "{spec}"'
    )


def _refuse_local_runtime(reason: str, detail: dict) -> None:
    """Record why local mode cannot start, with the command that fixes it.

    The next SessionStart shows the marker as a systemMessage (see
    ``_apply_host_python_warning``) until a usable runtime clears it.
    """
    message = (
        "Cognee Memory: local mode needs Cognee installed by you; the plugin does not "
        f"download or install packages. {reason} Run (with Python 3.10-3.14):\n"
        f"  {manual_install_command()}\n"
        "then start a new session. Cognee Cloud needs no local install."
    )
    hook_log("local_runtime_unavailable", {"reason": reason, **detail})
    print(message, file=sys.stderr)
    try:
        _COGNEE_INSTALL_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _COGNEE_INSTALL_MARKER.write_text(
            json.dumps({"message": message, "updated_at": time.time(), **detail}),
            encoding="utf-8",
        )
    except Exception as exc:
        hook_log("cognee_install_marker_write_failed", {"error": str(exc)[:200]})


def _flag_missing_local_runtime() -> None:
    """Say at once when local mode has no venv at all (a file check, no probe).

    A venv that exists but is too old or lacks extras is found by the boot path
    (``ensure_cognee_installed``), which records the same marker.
    """
    try:
        from _cloud_worker import enabled as cloud_enabled
        from _plugin_common import managed_endpoint_enabled, service_url_is_local

        if cloud_enabled() or managed_endpoint_enabled() or not service_url_is_local():
            return
        if _VENV_PYTHON.exists() or _COGNEE_INSTALL_MARKER.exists():
            return
        _refuse_local_runtime(
            "Cognee is not installed in ~/.cognee-plugin/venv.", {"installed": ""}
        )
    except Exception as exc:
        hook_log("local_runtime_check_failed", {"error": str(exc)[:200]})


def ensure_cognee_installed(timeout: float = 0.0) -> bool:
    """Whether the user-built venv holds a usable cognee. Never installs (SDK-1411).

    Usable: cognee ``_PINNED_COGNEE_VERSION`` or newer (the version this plugin
    release is tested with is the floor), with the driver extras the configured
    providers need. Anything else is recorded with the exact install command and
    local mode stays off; the server is never booted from an unusable venv.
    """
    apply_cognee_env()
    for directory in (_COGNEE_SYSTEM_DIR, _COGNEE_DATA_DIR, _COGNEE_CACHE_DIR):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            hook_log(
                "cognee_data_dir_mkdir_failed", {"dir": str(directory), "error": str(exc)[:200]}
            )
    current = _venv_cognee_version()
    if not current:
        _refuse_local_runtime(
            "Cognee is not installed in ~/.cognee-plugin/venv.", {"installed": ""}
        )
        return False
    if _version_tuple(current) < _version_tuple(_PINNED_COGNEE_VERSION):
        _refuse_local_runtime(
            f"Installed cognee {current} is older than {_PINNED_COGNEE_VERSION}.",
            {"installed": current},
        )
        return False
    required_extras = _detect_required_extras()
    missing_extras = _venv_missing_extras(required_extras)
    if missing_extras:
        _refuse_local_runtime(
            "Your configured providers need driver extras that are not installed: "
            + ", ".join(missing_extras)
            + ".",
            {"installed": current, "missing_extras": missing_extras},
        )
        return False
    _write_venv_ready(current)
    hook_log("cognee_runtime_ready", {"version": current, "extras": required_extras})
    return True


def _parse_host_port(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(url if "://" in url else f"http://{url}")
    return (parsed.hostname or "localhost"), (parsed.port or 8011)


def _is_local_url(url: str) -> bool:
    """True if the URL points at this machine (so we may boot a server on it)."""
    host, _ = _parse_host_port(url)
    return is_loopback_host(host)


def _with_scheme(url: str) -> str:
    """Ensure the URL has a scheme so urllib + downstream HTTP helpers accept it."""
    url = str(url or "").strip()
    if url and "://" not in url:
        url = f"http://{url}"
    return url.rstrip("/")


def _health_url(service_url: str) -> str:
    return f"{_with_scheme(service_url or _LOCAL_SERVICE_URL)}/health"


def _health_ok(url: str = _HEALTH_URL, timeout: float = 2.0) -> bool:
    try:
        with open_url(url, timeout=timeout, context=_https_context()) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _wait_for_health(deadline_seconds: float, health_url: str = _HEALTH_URL) -> bool:
    """Poll /health until the server is serving or the deadline elapses.

    Used by bootstrap workers that did not win the boot single-flight: they
    don't spawn uvicorn, they just wait for whichever worker did to finish
    booting (including migrations) before registering.
    """
    deadline = time.monotonic() + deadline_seconds
    while True:
        if _health_ok(health_url):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_HEALTH_POLL_SECONDS)


# What the local server's environment may carry over from this process: the
# platform basics (path, home, locale, temp, proxy, TLS, Windows system vars).
# Nothing else is copied, so a provider key, database password or token in the
# user's shell never reaches the server; its LLM and embedding settings come
# from the plugin options (see _env_file) or the Claude observer.
_SERVER_ENV_BASE = frozenset(
    name.upper()
    for name in (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        # cognee's own usage-telemetry opt-out: a non-secret switch the user set.
        "TELEMETRY_DISABLED",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "__CF_USER_TEXT_ENCODING",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "USERNAME",
        "USERDOMAIN",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "COMMONPROGRAMFILES",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
        "COMPUTERNAME",
    )
)
_PROXY_NAMES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"})
_LOOPBACK_NO_PROXY = ("127.0.0.1", "localhost", "::1")
# Server settings the plugin itself puts in its environment (apply_cognee_env).
_SERVER_ENV_PLUGIN = frozenset(
    {
        "SYSTEM_ROOT_DIRECTORY",
        "DATA_ROOT_DIRECTORY",
        "CACHE_ROOT_DIRECTORY",
        "CACHING",
        "AUTO_FEEDBACK",
        "DISTILLATION_MODE",
        "COGNEE_LLM_OBSERVER_ACTIVE",
    }
)
# An empty file the plugin owns, handed to cognee as COGNEE_ENV_FILE so the
# server loads exactly it and never searches upward from its working directory
# for some other .env (which would override the settings above). cognee treats
# a missing COGNEE_ENV_FILE as an error, so it is (re)created before each boot.
_SERVER_ENV_FILE = _STATE_DIR / "server.env"


def _server_env() -> dict:
    """The local server's environment, built from allowlists. Never copies os.environ.

    Holds the platform basics, the plugin's own server settings and tuning
    names, the non-secret LLM / embedding settings (the plugin options, or the
    observer's shim settings, which are the only ``LLM_*`` / ``EMBEDDING_*``
    values left in this process), the keys (the observer's shim token or the
    LLM and embedding key options, read here and placed in this environment
    only, never taken from this process's own), the default-user login,
    ``COGNEE_ENV_FILE`` and ``HASH_API_KEY=false`` (the owner-key lookup needs
    the stored key back).
    """
    from _env_file import (
        _TUNING_NAMES,
        LLM_OPTIONS,
        OPTION_VARS,
        SENSITIVE_OPTIONS,
        server_secret_env,
    )
    from _observer import server_secret_env as observer_secret_env

    key_names = {OPTION_VARS[name] for name in SENSITIVE_OPTIONS if name in LLM_OPTIONS}
    env: dict = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in key_names:
            # A key comes from the two sources below only.
            continue
        if (
            upper in _SERVER_ENV_BASE
            or upper.startswith("LC_")
            or name in _SERVER_ENV_PLUGIN
            or name in _TUNING_NAMES
            or name.startswith(("LLM_", "EMBEDDING_"))
        ):
            env[name] = value
    # The observer's own shim token, when the observer is this server's LLM,
    # then the user's LLM and embedding keys, straight from the options. The
    # token keeps priority: without it the server's calls to the shim are
    # refused.
    env.update(observer_secret_env())
    for name, value in server_secret_env().items():
        env.setdefault(name, value)
    # The server calls this machine too (the observer shim, with its bearer
    # token and the full prompt). With a proxy copied over, exempt loopback in
    # both spellings so those calls never reach the proxy host, keeping the
    # user's own entries.
    if any(name.upper() in _PROXY_NAMES for name in env):
        own = env.get("no_proxy") or env.get("NO_PROXY") or ""
        entries = [e.strip() for e in own.split(",") if e.strip()]
        for host in _LOOPBACK_NO_PROXY:
            if host not in entries:
                entries.append(host)
        env["NO_PROXY"] = ",".join(entries)
        # Windows names are case-insensitive: one spelling there.
        if sys.platform != "win32":
            env["no_proxy"] = env["NO_PROXY"]
    # Run in agent mode: the server tears itself down once all registered
    # agents disconnect.
    env["COGNEE_AGENT_MODE"] = "true"
    # cognee >= 1.6.0 creates the default user at startup only when
    # DEFAULT_USER_PASSWORD is set, and never rewrites a stored password. Hand
    # the server the well-known local credentials so a fresh install gets the
    # same default user it always had and the owner-key lookup can log in. The
    # server binds localhost only.
    env["DEFAULT_USER_EMAIL"] = _LOCAL_DEFAULT_USER_EMAIL
    env["DEFAULT_USER_PASSWORD"] = _LOCAL_DEFAULT_USER_PASSWORD
    env["HASH_API_KEY"] = "false"
    try:
        _SERVER_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SERVER_ENV_FILE.write_text("", encoding="utf-8")
        env["COGNEE_ENV_FILE"] = str(_SERVER_ENV_FILE)
    except OSError as exc:
        hook_log("server_env_file_failed", {"error": str(exc)[:200]})
    return env


def _ensure_local_server_running(
    config: dict, health_timeout: float = _HEALTH_TIMEOUT_SECONDS
) -> None:
    # Target the configured local URL (any port) or the default; boot uvicorn on
    # that URL's port if it's not already serving.
    service_url = _with_scheme(config.get("base_url", "") or _LOCAL_SERVICE_URL)
    health_url = _health_url(service_url)
    _, port = _parse_host_port(service_url)

    def _ready() -> None:
        config["base_url"] = service_url
        os.environ["COGNEE_BASE_URL"] = service_url

    if _health_ok(health_url):
        _ready()
        return

    # A missed probe is NOT absence: a server that is busy (cognifying, event
    # loop saturated) misses the health deadline exactly like a dead one, but
    # only a dead one may be installed/booted over — a busy server still holds
    # the graph store's file lock, and upgrading + migrating under it corrupts
    # the databases (2026-08-13 incident). Only a positively-absent verdict
    # from the full-evidence presence check licenses going further.
    def _require_absent(stage: str) -> bool:
        """True when the server turned out to be serving (caller returns);
        raises unless it is positively absent."""
        verdict, evidence = server_presence(service_url, confirm_absent=(stage == "pre_install"))
        if verdict == PRESENCE_READY:
            return True
        if verdict != PRESENCE_ABSENT:
            hook_log(
                "boot_refused_server_present",
                {"stage": stage, "verdict": verdict, **evidence},
            )
            raise RuntimeError(
                f"server at {service_url} is present but not serving "
                f"(verdict={verdict}, stage={stage}); refusing to install or boot over it"
            )
        return False

    if _require_absent("pre_install"):
        _ready()
        return

    if managed_endpoint_enabled(config):
        # Hard stop at the single spawn/install choke point so no call path —
        # present or future — can boot a shadow server over a managed endpoint.
        hook_log("managed_endpoint_boot_refused", {"base_url": service_url})
        raise RuntimeError(
            f"managed Cognee endpoint {service_url} is unreachable and "
            "COGNEE_MANAGED_ENDPOINT forbids booting a local fallback server"
        )

    # The server is positively absent and we're at a boot point: check that the
    # user-built venv holds a usable cognee BEFORE booting. Nothing is installed
    # (SDK-1411); an unusable venv is reported with the command that fixes it.
    if not ensure_cognee_installed():
        raise RuntimeError("cognee runtime unavailable (install cognee, see the session message)")

    owner = f"session-start:{os.getpid()}"
    acquired = False
    deadline = time.monotonic() + _SERVER_BOOT_LOCK_WAIT_SECONDS
    try:
        _SERVER_BOOT_LOCK.parent.mkdir(parents=True, exist_ok=True)
        while True:
            now = time.time()
            if _SERVER_BOOT_LOCK.exists():
                stale = False
                try:
                    raw = json.loads(_SERVER_BOOT_LOCK.read_text(encoding="utf-8"))
                    pid = int(raw.get("pid", 0) or 0)
                    created_at = float(raw.get("created_at", 0) or 0)
                    stale = (not _pid_alive(pid)) or (
                        now - created_at > _SERVER_BOOT_LOCK_STALE_SECONDS
                    )
                except Exception:
                    stale = True
                if stale:
                    try:
                        _SERVER_BOOT_LOCK.unlink()
                        hook_log("server_bootstrap_lock_stale_reaped", {"owner": owner})
                    except Exception as exc:
                        hook_log("server_bootstrap_lock_unlink_failed", {"error": str(exc)[:200]})

            try:
                fd = os.open(str(_SERVER_BOOT_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"owner": owner, "pid": os.getpid(), "created_at": now}, fh)
                acquired = True
                hook_log("server_bootstrap_lock_acquired", {"owner": owner})
                break
            except FileExistsError:
                if _health_ok(health_url):
                    _ready()
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError("server bootstrap lock timeout")
                time.sleep(_SERVER_BOOT_LOCK_POLL_SECONDS)

        # Final re-check under the lock: waiting for it may have taken a while,
        # and only a still-absent server may be spawned over the port.
        if _require_absent("in_lock"):
            _ready()
            return

        server_env = _server_env()
        # The server's console output must not inherit this worker's stdio
        # (bootstrap.log — that is how it reached gigabytes: every line the
        # server printed for as long as it ran), but it must not vanish either:
        # a boot that dies before cognee opens its own log (import error,
        # broken venv, port bind) explains itself only on stderr. So it goes to
        # a capture pump that keeps the first megabyte of each boot in
        # server-console.log (previous boot in .1) and discards the rest.
        console_log = _STATE_DIR / "server-console.log"
        pump = _start_console_capture(console_log)
        if pump is None:
            hook_log("server_console_capture_unavailable", {"path": str(console_log)})
        server_proc = subprocess.Popen(
            [str(_VENV_PYTHON), "-m", "uvicorn", "cognee.api.client:app", "--port", str(port)],
            # Import the installed runtime even when the host opened a Cognee checkout.
            cwd=str(_VENV_DIR),
            env=server_env,
            stdin=subprocess.DEVNULL,
            stdout=pump.stdin if pump else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if pump else subprocess.DEVNULL,
            start_new_session=True,
        )
        if pump is not None and pump.stdin is not None:
            pump.stdin.close()  # the server holds the write end now
        # Presence evidence for future boot points: covers the spawn-to-bind
        # window where neither the health probe nor the TCP listener sees it.
        write_server_pidfile(
            port,
            server_proc.pid,
            version=_PINNED_COGNEE_VERSION,
            llm_observer=server_env.get("COGNEE_LLM_OBSERVER_ACTIVE", "") == "1",
            coding_memory=server_env.get("DISTILLATION_MODE", "").strip().lower() == "coding",
        )

        health_deadline = time.monotonic() + health_timeout
        while time.monotonic() < health_deadline:
            if _health_ok(health_url):
                _ready()
                return
            exit_code = server_proc.poll()
            if exit_code is not None:
                clear_server_pidfile(port)
                # An immediate exit is usually a failed port bind — something
                # claimed the port between our absence check and the spawn. If
                # that something is serving, connect to it instead of failing.
                if _health_ok(health_url):
                    _ready()
                    return
                tail = _console_capture_tail(console_log)
                hook_log(
                    "server_exited_before_healthy", {"rc": exit_code, "console_tail": tail[-600:]}
                )
                raise RuntimeError(
                    f"server process exited (rc={exit_code}) before becoming "
                    f"healthy at {health_url}; console: {console_log}"
                    + (f"\n{tail}" if tail else "")
                )
            time.sleep(_HEALTH_POLL_SECONDS)

        raise RuntimeError(
            f"Cognee server did not become healthy at {health_url} within {health_timeout}s; "
            f"console: {console_log}; server log: newest file in {_COGNEE_HOME / 'logs'}"
        )
    finally:
        if acquired:
            try:
                _SERVER_BOOT_LOCK.unlink()
                hook_log("server_bootstrap_lock_released", {"owner": owner})
            except Exception as exc:
                hook_log("server_bootstrap_lock_release_failed", {"error": str(exc)[:200]})


def _normalize_service_url(service_url: str) -> str:
    return str(service_url or "").strip().rstrip("/")


# The credentials a server booted by this plugin is given (DEFAULT_USER_EMAIL /
# DEFAULT_USER_PASSWORD) and the ones config.py logs in with by default. They
# must agree, or a fresh install cannot mint its owner API key.
_LOCAL_DEFAULT_USER_EMAIL = "default_user@example.com"
_LOCAL_DEFAULT_USER_PASSWORD = "default_password"
_NO_PASSWORD_MARKER = "does not have a password"
_BAD_CREDENTIALS_MARKER = "LOGIN_BAD_CREDENTIALS"
# Left when the local server was found to hide its keys, so session start can
# say so (the finding may come from the detached bootstrap worker, after this
# session's output went out). Removed once a usable owner key resolves.
_HIDES_KEYS_MARKER = _STATE_DIR / "local-server-hides-keys.json"
SERVER_HIDES_KEYS_MESSAGE = (
    "This local Cognee server hides its API keys (HASH_API_KEY=true), so the plugin "
    "can't use it. Stop it and start a new session; the plugin starts its own server."
)


def _login_failure_message(status: int, body: str) -> str:
    """One actionable sentence for a failed default-user login.

    cognee >= 1.6.0 creates the default user without a password unless the server
    was started with DEFAULT_USER_PASSWORD, and logging into such a user answers
    400 "does not have a password". A server this plugin boots gets the variable
    itself, so that answer (or a rejected login) means a server the plugin did
    not start, whose environment the plugin cannot set.
    """
    head = f"default-user login failed ({status}: {body[:200]})."
    own_server = (
        "The plugin logs in to a local server as its default user "
        f"({_LOCAL_DEFAULT_USER_EMAIL}). To use a server you run yourself, set its "
        "address and an API key in the plugin's options (/plugin → cognee-memory: "
        "Cognee Cloud URL and Cognee Cloud API key), or stop it so the plugin starts its own."
    )
    if status == 400 and _NO_PASSWORD_MARKER in body:
        return (
            f"{head} The server's default user has no password: cognee >= 1.6.0 "
            "creates none unless the server is started with DEFAULT_USER_PASSWORD set. "
            + own_server
        )
    if status == 400 and _BAD_CREDENTIALS_MARKER in body:
        return f"{head} The server rejected the default user's password. " + own_server
    return f"{head} " + own_server


async def _login_default_user_for_owner_api_key(
    service_url: str, config: dict, *, mint: bool = False
) -> str:
    """Log in as the local server's default user and return its owner API key.

    Reuses the first listed key that is a real key (not masked, not a stored
    hash), the same one every other hook process finds; mints one when there is
    none, or always when ``mint`` (the listed one was rejected). A server that
    masks every key it lists is refused instead: a key minted there could not
    reach the other hooks, since nothing is stored on disk.
    """
    from _plugin_common import first_usable_key, server_hides_keys

    base = _normalize_service_url(service_url)
    email = config.get("user_email", "")
    password = config.get("user_password", "")

    status, body = _cloud_http_request(
        f"{base}/api/v1/auth/login",
        method="POST",
        form_body={"username": email, "password": password},
        timeout=30.0,
    )
    if status != 200:
        raise RuntimeError(_login_failure_message(status, body))
    login_data = json.loads(body) if body else {}
    jwt = str(login_data.get("access_token", "") or "")
    if not jwt:
        raise RuntimeError("default-user login returned no access token")

    if not mint:
        status, body = _cloud_http_request(
            f"{base}/api/v1/auth/api-keys",
            method="GET",
            cookies={"auth_token": jwt},
            timeout=30.0,
        )
        if status == 200:
            listed = json.loads(body) if body else []
            key = first_usable_key(listed)
            if key:
                return key
            if server_hides_keys(listed):
                hook_log("local_server_hides_keys", {"base_url": base})
                try:
                    _HIDES_KEYS_MARKER.parent.mkdir(parents=True, exist_ok=True)
                    _HIDES_KEYS_MARKER.write_text(
                        json.dumps({"base_url": base, "found_at": time.time()}), encoding="utf-8"
                    )
                except OSError:
                    pass
                raise RuntimeError(SERVER_HIDES_KEYS_MESSAGE)

    status, body = _cloud_http_request(
        f"{base}/api/v1/auth/api-keys",
        method="POST",
        json_body={"name": "claude-owner-bootstrap"},
        cookies={"auth_token": jwt},
        timeout=30.0,
    )
    if status != 200:
        raise RuntimeError(f"default-user API key creation failed ({status}: {body[:200]})")
    payload = json.loads(body) if body else {}
    key = str(payload.get("key", "") or "")
    if not key:
        raise RuntimeError("default-user API key creation returned empty key")
    hook_log("local_owner_key_minted", {"replaced_rejected": bool(mint)})
    return key


def _local_key_rejected(service_url: str, api_key: str) -> bool:
    """True only when the server answers the key with 401/403 (one check)."""
    try:
        status, _body = _cloud_http_request(
            f"{_normalize_service_url(service_url)}/api/v1/users/me",
            api_key=api_key,
            timeout=10.0,
        )
    except Exception:
        return False
    return status in (401, 403)


def _resolve_agent_name(config: dict, cwd: str) -> str:
    def _normalize(name: str) -> str:
        raw = str(name or "").strip()
        if raw.endswith("@cognee.agent"):
            raw = raw[: -len("@cognee.agent")]
        suffix = "_claude"
        if raw.endswith(suffix):
            return raw
        return f"{raw}{suffix}"

    configured = str(config.get("agent_name", "") or "").strip()
    if configured:
        return _normalize(configured)
    return _normalize(f"claude-{Path(cwd).name}")


async def _resolve_single_principal_key(service_url: str, config: dict) -> str:
    """Resolve the one key every request runs as.

    Remote: the user's key, read from the plugin option. Local: never the option key, but
    the server's own owner key, looked up from that server and checked once;
    minted there on a fresh install, or when the listed key is rejected (a key
    stored hashed by an earlier ``HASH_API_KEY=true`` server). Nothing is
    written to disk.
    """
    from _plugin_common import (
        cloud_key_applies,
        forget_cached_api_key,
        load_cached_api_key,
        save_cached_api_key,
        takes_owner_key,
    )

    if cloud_key_applies(service_url):
        return _api_key_with_source(service_url)[0]
    if not takes_owner_key(service_url):
        # Pinned to cloud with no Cloud URL option for this address: no key,
        # and nothing is looked up from, or logged into, a local server.
        return ""

    api_key = load_cached_api_key(service_url)
    if api_key and _local_key_rejected(service_url, api_key):
        hook_log("local_owner_key_rejected", {"base_url": service_url})
        forget_cached_api_key(service_url)
        api_key = await _login_default_user_for_owner_api_key(service_url, config, mint=True)
    elif not api_key:
        api_key = await _login_default_user_for_owner_api_key(service_url, config)
    if api_key:
        save_cached_api_key(service_url, api_key)
        try:
            _HIDES_KEYS_MARKER.unlink()
        except OSError:
            pass
    return api_key


async def _ensure_agent_credentials_and_register(
    config: dict, cwd: str, session_id: str, agent_session_name: str, session_key: str
) -> tuple[str, str, str, bool]:
    """Register this session as the user (no plugin identity in this plugin)."""
    service_url = _normalize_service_url(str(config.get("base_url", "") or ""))
    if not service_url:
        return "", "", "", False

    from _plugin_common import register_agent_via_http, set_launch_dataset_ids

    api_key = await _resolve_single_principal_key(service_url, config)
    if not api_key:
        return "", "", "", False

    config["api_key"] = api_key
    # No agent, so no shared-memory role: the launch goes by dataset name.
    set_launch_dataset_ids(session_key, "", [])

    user_id = await _user_id_via_api(service_url, api_key)

    # Without a canonical id the dataset goes by NAME, which the server
    # resolves only among the caller's existing datasets (cognee >= 1.6.3): a
    # name that does not exist yet is refused with 403, and on a new account
    # that is the default dataset. Create it first. Best-effort: when it fails,
    # the registration reports the real error.
    dataset = str(config.get("dataset", "") or "").strip()
    try:
        await ensure_dataset_ready_via_api(service_url, api_key, dataset)
    except Exception as exc:
        hook_log("dataset_ensure_before_register_failed", {"error": str(exc)[:200]})
    registered, registration = register_agent_via_http(
        agent_session_name=agent_session_name,
        session_id=session_id,
        dataset_names=[dataset],
        dataset_ids=None,
    )
    # Optional lifecycle routes (older / minimal servers, lifecycle_supported is
    # False): the session runs unregistered rather than failing. Anything else
    # is a real failure on a server that has the route — raise with the HTTP
    # status so the caller can classify it (auth vs server error).
    if not registered and registration.get("lifecycle_supported") is not False:
        status = registration.get("status_code")
        message = f"Failed to register session '{session_id}' on {service_url}."
        if registration.get("dataset_refused"):
            # The key works; the server refused the dataset. Raised without an
            # HTTP status so the status line does not report an auth failure.
            raise RuntimeError(f"{message} The server refused the dataset.")
        if status:
            raise urllib.error.HTTPError(service_url, status, message, None, None)
        raise RuntimeError(message)

    hook_log(
        "agent_register_result",
        {
            "agent_session_name": agent_session_name,
            "registered": registered,
            "connection_id": str(registration.get("id", "")),
            "session_id": session_id,
            "user_id": user_id,
        },
    )

    return user_id, api_key, agent_session_name, registered


def _watcher_alive() -> bool:
    if not _WATCHER_PID.exists():
        return False
    try:
        pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    return _pid_alive(pid)


def _spawn_idle_watcher(
    session_id: str, dataset: str, user_id: str, config: dict, session_key: str
) -> None:
    """Launch the idle watcher as a detached background process.

    Idempotent: if a watcher is already alive (from an earlier session
    on the same machine), we kill it so the new one picks up the new
    session. Launched with its own session via ``start_new_session=True``
    so it survives the parent shell closing.
    """
    if _watcher_alive():
        try:
            pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            hook_log("idle_watcher_kill_failed", {"error": str(exc)[:200]})

    # Clear any stale stop sentinel from a previous run.
    try:
        if _WATCHER_STOP.exists():
            _WATCHER_STOP.unlink()
    except Exception as exc:
        hook_log("watcher_stop_unlink_failed", {"error": str(exc)[:200]})

    # Only the non-secret surface of config needs to travel — the
    # watcher re-runs ``ensure_cognee_ready`` on its own.
    bootstrap = {
        "session_id": session_id,
        "dataset": dataset,
        "user_id": user_id,
        "session_key": session_key,
        "config": {
            "base_url": config.get("base_url", ""),
            "llm_model": config.get("llm_model", ""),
            "dataset": dataset,
        },
    }

    log_path = _STATE_DIR / "watcher.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log_if_oversized(log_path)  # the child writes it; cap it at handover
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception as exc:
        hook_log("watcher_log_open_failed", {"error": str(exc)[:200]})
        log_fh = subprocess.DEVNULL

    try:
        env = os.environ.copy()
        if session_key:
            env["COGNEE_SESSION_KEY"] = session_key
        subprocess.Popen(
            [sys.executable, str(_WATCHER_SCRIPT), json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        print("cognee-plugin: idle watcher started", file=sys.stderr)
    except Exception as e:
        print(f"cognee-plugin: idle watcher launch failed ({e})", file=sys.stderr)


def _find_claude_parent_pid() -> int:
    """Find the live Claude host process for this hook.

    Claude Code exports its own pid as ``CLAUDE_PID`` to every subprocess; when
    that pid is alive it IS the host, so use it before walking the process tree.
    The walk below stops at the *nearest* ``claude`` ancestor, which on the
    Windows desktop app is the short-lived hook-runner rather than the session
    runtime: the exit watcher then sees ``parent_exited`` within a second of
    session start, runs the final sync early and unregisters the live connection
    (topoteretes/cognee-integrations#391).
    """
    try:
        host_pid = int(str(os.environ.get("CLAUDE_PID", "") or "0").strip() or 0)
    except ValueError:
        host_pid = 0
    if host_pid > 1 and _pid_alive(host_pid):
        return host_pid
    fallback = os.getppid()
    if sys.platform == "win32":
        return find_host_ancestor_windows(fallback, "claude")
    try:
        raw = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        hook_log("find_claude_parent_failed", {"error": str(exc)[:200]})
        return fallback

    table: dict[int, tuple[int, str]] = {}
    for line in raw.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, parts[2])

    import re

    # Match "claude" as an executable basename anywhere in the command line,
    # tolerant of spaces in the executable path (a naive split()[0] mis-tokenizes
    # paths like "/…/Application Support/…/claude").
    host_re = re.compile(r"(?:^|/)claude(?:-[\w.]+)?(?:\s|$)")
    pid = fallback
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        ppid, command = table.get(pid, (0, ""))
        if command and host_re.search(command):
            return pid
        pid = ppid
    return fallback


def _spawn_exit_watcher(
    session_id: str,
    dataset: str,
    *,
    session_key: str = "",
    agent_session_name: str = "",
    api_key: str = "",
    service_url: str = "",
) -> None:
    """Launch a detached watcher that syncs only after Claude exits."""

    # Cleanup stale watcher pidfiles so the directory does not grow forever.
    try:
        if _EXIT_WATCHERS_DIR.exists():
            for pidfile in _EXIT_WATCHERS_DIR.glob("*.pid"):
                try:
                    pid = int(pidfile.read_text(encoding="utf-8").strip())
                    if not _pid_alive(pid):
                        pidfile.unlink()
                except Exception:
                    continue
    except Exception as exc:
        hook_log("exit_watcher_prune_failed", {"error": str(exc)[:200]})

    parent_pid = _find_claude_parent_pid()
    watcher_pidfile = _EXIT_WATCHERS_DIR / f"{parent_pid}.pid"
    try:
        if watcher_pidfile.exists():
            existing = int(watcher_pidfile.read_text(encoding="utf-8").strip())
            if _pid_alive(existing):
                hook_log(
                    "exit_watcher_already_running",
                    {"parent_pid": parent_pid, "pidfile": str(watcher_pidfile)},
                )
                return
    except Exception:
        pass

    bootstrap = {
        "parent_pid": parent_pid,
        "session_id": session_id,
        "dataset": dataset,
        "session_key": session_key,
        "agent_session_name": agent_session_name,
        "base_url": service_url,
        "pidfile": str(watcher_pidfile),
    }
    log_path = _STATE_DIR / "exit-watcher.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _EXIT_WATCHERS_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_log_if_oversized(log_path)  # the child writes it; cap it at handover
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception as exc:
        hook_log("exit_watcher_log_open_failed", {"error": str(exc)[:200]})
        log_fh = subprocess.DEVNULL

    try:
        # No key is handed over: the watcher reads the Cloud key option it
        # inherits, or looks the local owner key up from the server itself.
        env = os.environ.copy()
        if session_key:
            env["COGNEE_SESSION_KEY"] = session_key
        subprocess.Popen(
            [sys.executable, str(_EXIT_WATCHER_SCRIPT), json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        hook_log(
            "exit_watcher_started",
            {
                "parent_pid": parent_pid,
                "session_id": session_id,
                "dataset": dataset,
                "pidfile": str(watcher_pidfile),
            },
        )
    except Exception as e:
        hook_log("exit_watcher_launch_failed", {"error": str(e)[:300]})


def _purge_legacy_resolved_files() -> None:
    legacy = _STATE_DIR / "resolved.json"
    scoped_dir = _STATE_DIR / "resolved"
    # config.json: an older config layer that SessionStart read but the per-turn
    # hooks never did, so a stale base_url in it split the plugin across two
    # servers (SDK-466). Nothing reads it any more; remove it so it cannot be
    # mistaken for live configuration. The plugin options are the setup now.
    legacy_config = _STATE_DIR / "config.json"
    try:
        if legacy_config.exists():
            legacy_config.unlink()
            hook_log("legacy_config_json_removed", {"path": str(legacy_config)})
    except Exception as exc:
        hook_log("legacy_config_json_unlink_failed", {"error": str(exc)[:200]})
    try:
        if legacy.exists():
            legacy.unlink()
    except Exception as exc:
        hook_log("legacy_resolved_unlink_failed", {"error": str(exc)[:200]})
    try:
        if scoped_dir.exists():
            shutil.rmtree(scoped_dir)
    except Exception as exc:
        hook_log("legacy_resolved_dir_remove_failed", {"error": str(exc)[:200]})


@contextmanager
def _bootstrap_singleflight():
    """Allow exactly one detached bootstrap worker at a time (machine-wide).

    The marker is global because Claude and Codex share one local server, so
    only one of them should drive the boot + migration at any moment.
    """
    acquired = False
    try:
        _BOOTSTRAP_LOCK.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        if _BOOTSTRAP_LOCK.exists():
            stale = False
            try:
                raw = json.loads(_BOOTSTRAP_LOCK.read_text(encoding="utf-8"))
                pid = int(raw.get("pid", 0) or 0)
                created_at = float(raw.get("created_at", 0) or 0)
                stale = (not _pid_alive(pid)) or (now - created_at > _BOOTSTRAP_LOCK_STALE_SECONDS)
            except Exception:
                stale = True
            if stale:
                try:
                    _BOOTSTRAP_LOCK.unlink()
                except Exception as exc:
                    hook_log("bootstrap_lock_unlink_failed", {"error": str(exc)[:200]})
        try:
            fd = os.open(str(_BOOTSTRAP_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "created_at": now}, fh)
            acquired = True
        except FileExistsError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                _BOOTSTRAP_LOCK.unlink()
            except Exception as exc:
                hook_log("bootstrap_lock_release_failed", {"error": str(exc)[:200]})


def _spawn_bootstrap(
    config: dict,
    cwd: str,
    session_id: str,
    agent_session_name: str,
    session_key: str,
    dataset: str,
) -> bool:
    """Launch the detached server-bootstrap worker (this script, --bootstrap)."""
    bootstrap = {
        "cwd": cwd,
        "session_id": session_id,
        "session_key": session_key,
        "dataset": dataset,
        "agent_session_name": agent_session_name,
        "base_url": str(config.get("base_url", "") or _LOCAL_SERVICE_URL),
    }
    log_path = _STATE_DIR / "bootstrap.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log_if_oversized(log_path)  # the child writes it; cap it at handover
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception as exc:
        hook_log("bootstrap_log_open_failed", {"error": str(exc)[:200]})
        log_fh = subprocess.DEVNULL
    try:
        env = os.environ.copy()
        if session_key:
            env["COGNEE_SESSION_KEY"] = session_key
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), _BOOTSTRAP_ARG, json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        hook_log("bootstrap_spawned", {"session_id": session_id})
        return True
    except Exception as exc:
        hook_log("bootstrap_spawn_failed", {"error": str(exc)[:300]})
        return False


def _status_from_error(message: str) -> int:
    """Extract an HTTP status embedded as ``(<status>: ...)`` in an error string.

    ``ensure_cognee_ready`` and ``ensure_dataset_ready_via_api`` both format their
    RuntimeError as ``... (<status>: <body>)``; return 0 when none is present
    (e.g. a connection error carries no HTTP status).
    """
    import re

    if isinstance(message, urllib.error.HTTPError):
        return message.code
    m = re.search(r"\((\d{3})[:\s)]", str(message or ""))
    return int(m.group(1)) if m else 0


def _classify_conn_status(status: int) -> str:
    """Map an HTTP status to a connection-failure state."""
    if status in (401, 403):
        return "auth_failed"
    if status >= 500:
        return "server_error"
    return "unreachable"


async def _run_heavy(
    config: dict,
    cwd: str,
    session_id: str,
    agent_session_name: str,
    session_key: str,
    dataset: str,
    *,
    managed_endpoint: bool,
    boot_timeout: float,
    on_registered: Callable[[], None] | None = None,
) -> tuple[str, str, bool]:
    """Slow session bootstrap shared by the inline (legacy) path and the
    detached worker: server boot wait, cognee init, agent registration, dataset
    creation, and the server-ready marker.

    ``on_registered`` runs once the session is registered and the connection
    state recorded, before the code-graph pass: memory is usable from then on,
    and the worker uses it to say so without waiting for the index.

    Returns ``(user_id, agent_api_key, ok)``. ``ok`` is False only on a hard
    cloud-registration failure (mirrors the legacy early-abort).
    """
    if not managed_endpoint:
        try:
            _ensure_local_server_running(config, health_timeout=boot_timeout)
        except Exception as exc:
            hook_log("server_bootstrap_warning", {"error": str(exc)[:200]})

    # On a cold start this worker began under the host python3, so the
    # _plugin_common import-time guard could not re-exec us. The boot above
    # (via ensure_cognee_installed) has now built the venv, so flip into it so
    # the rest of this bootstrap runs under the plugin-owned interpreter. No-op
    # when the venv is absent (connect/managed mode never builds one) or when
    # we are already inside it (warm start).
    _reexec_into_venv()

    # Track the cloud connection outcome so the status line can show a precise
    # "✕ (<reason>)". Stays None in local mode (handled by the health probe at
    # the tail).
    _conn_state = None
    _conn_detail = ""

    # Configure cognee (cloud or local)
    try:
        await ensure_cognee_ready(config)
    except Exception as e:
        print(f"cognee-plugin: init warning ({e})", file=sys.stderr)
        if is_cloud_mode(config):
            status = _status_from_error(e)
            _conn_state = _classify_conn_status(status) if status else "unreachable"
            _conn_detail = str(e)[:200]

    # Register agent identity.
    user_id = ""
    agent_api_key = ""
    agent_id = ""
    agent_name = _resolve_agent_name(config, cwd)
    os.environ["COGNEE_AGENT_NAME"] = agent_name

    # HTTP path: resolve the single principal key (env / cache / mint from the
    # default user) and register this session as an agent-mode connection.
    if is_cloud_mode(config):
        try:
            (
                agent_id,
                agent_api_key,
                agent_name,
                _registered,
            ) = await _ensure_agent_credentials_and_register(
                config, cwd, session_id, agent_session_name, session_key
            )
            if agent_id:
                user_id = agent_id
        except Exception as exc:
            message = str(exc)[:300]
            hook_log("agent_lifecycle_error", {"error": message})
            print(f"cognee-plugin: agent lifecycle failed ({message})", file=sys.stderr)
            # Classify for the status line. Preserve an earlier health-derived
            # state; otherwise a reachable server rejecting registration is almost
            # always auth, while a positively-absent one is a connection failure.
            # A timed-out probe is NO verdict (busy != down): keep the prior
            # recorded state rather than stamp a false "unreachable".
            status = _status_from_error(exc)
            if _conn_state is None and status:
                _conn_state, _conn_detail = _classify_conn_status(status), message
            if _conn_state is None:
                try:
                    health = probe_health(
                        _normalize_service_url(str(config.get("base_url", "") or "")), timeout=1.5
                    )
                except Exception:
                    health = "unknown"
                if health == "down":
                    _conn_state, _conn_detail = "unreachable", message
            if _conn_state:
                write_connection_state(
                    _conn_state, str(config.get("base_url", "") or ""), detail=_conn_detail
                )
            else:
                hook_log(
                    "conn_state_unverified",
                    {"reason": "probe returned no verdict after registration failure"},
                )
            return "", "", False

    try:
        # Cloud: the API key IS the identity (the server derives the principal
        # from X-Api-Key), so dataset creation must NOT be gated on user_id —
        # servers without /users/me (e.g. cloud tenants) leave user_id empty
        # while auth works fine.
        # Under shared memory the canonical dataset was already resolved (and
        # created as the parent when absent) by the credential step; creating
        # it here as the agent would fork an agent-owned copy of the same name.
        from _plugin_common import resolve_active_dataset_ids

        if is_cloud_mode(config) and not resolve_active_dataset_ids(session_key)[0]:
            await ensure_dataset_ready_via_api(
                config.get("base_url", ""),
                agent_api_key or config.get("api_key", ""),
                dataset,
            )
    except Exception as e:
        print(f"cognee-plugin: dataset warning ({e})", file=sys.stderr)
        if is_cloud_mode(config):
            status = _status_from_error(e)
            # An authed 401/403 here is a definitive auth failure — let it win.
            if status in (401, 403):
                _conn_state, _conn_detail = "auth_failed", str(e)[:200]
            elif status >= 500 and _conn_state is None:
                _conn_state, _conn_detail = "server_error", str(e)[:200]
    from _project_memory import prepare as prepare_project_memory

    project_state = prepare_project_memory(dataset, session_id)
    if project_state:
        hook_log("project_memory_prepared", project_state)
    if user_id:
        os.environ["COGNEE_USER_ID"] = user_id

    # Record the connection outcome so the status line shows "● " (ready) or
    # "✕ (<reason>)". Cloud: use the classified state gathered above, else confirm
    # ready. Local/managed: a health probe decides ready-vs-unreachable.
    service_url = _normalize_service_url(str(config.get("base_url", "") or ""))
    if not service_url and not managed_endpoint:
        service_url = _LOCAL_SERVICE_URL
    if is_cloud_mode(config):
        if _conn_state and _conn_state != "ready":
            write_connection_state(
                _conn_state, str(config.get("base_url", "") or ""), detail=_conn_detail
            )
        elif service_url:
            health = probe_health(service_url, timeout=1.5)
            if health == "ready":
                write_connection_state("ready", service_url)
            elif health == "down":
                write_connection_state(
                    "unreachable", str(config.get("base_url", "") or service_url)
                )
            else:
                # "slow"/"unknown": no verdict — keep whatever state is already
                # recorded instead of branding a busy server unreachable.
                hook_log("conn_state_unverified", {"base_url": service_url, "health": health})
    elif service_url and probe_health(service_url, timeout=1.5) == "ready":
        write_connection_state("ready", service_url)

    if on_registered is not None:
        try:
            on_registered()
        except Exception as exc:
            hook_log("phase_end_failed", {"phase": "setting_up", "error": str(exc)[:200]})

    # Code graph: index the repository this session opened in, or refresh it if
    # the tree moved while the agent was away. Deliberately last and inside the
    # detached worker — it must never delay the first prompt — and best-effort:
    # a failure here costs code facts, not the session.
    try:
        from _code_graph import autoindex_on_session_start

        if coding_memory_enabled():
            # Coding memory (SDK-1336) leaves the code graph alone.
            raise _SkipAutoindex()
        code_outcome = autoindex_on_session_start(
            cwd,
            service_url,
            _api_key_with_source(service_url)[0],
            is_local_server=service_url_is_local(service_url),
        )
        if code_outcome:
            hook_log("code_autoindex", code_outcome)
    except _SkipAutoindex:
        pass
    except Exception as exc:
        hook_log("code_autoindex_error", {"error": str(exc)[:200]})

    return user_id, agent_api_key, True


async def _run_bootstrap(bootstrap: dict) -> None:
    """Detached worker: set the session up (:func:`_bootstrap_session`).

    The worker is the last owner of the session's setup, so however it ends
    (refused, unhealthy server, an exception), a session ``prepare()`` never
    ran for is handed to a later ``route()``. A prepared or backed-off state
    is left alone (:func:`_project_memory_gave_up`).
    """
    try:
        await _bootstrap_session(bootstrap)
    finally:
        _project_memory_gave_up(
            str(bootstrap.get("dataset", "") or get_dataset(load_config())),
            str(bootstrap.get("session_id", "") or ""),
        )


async def _bootstrap_session(bootstrap: dict) -> None:
    """Detached worker body.

    Two distinct concerns, deliberately decoupled:
      1. Boot the local server EXACTLY ONCE — single-flighted on _BOOTSTRAP_LOCK
         so concurrent agents don't each spawn uvicorn. Workers that don't win
         the lock just wait for /health instead of returning.
      2. Register THIS agent/session — runs for EVERY agent, regardless of who
         booted, because registration is per-session (the connection handle is
         the Cognee session id) under the single principal.
    """
    config = load_config()
    cwd = str(bootstrap.get("cwd") or os.getcwd())
    session_id = str(bootstrap.get("session_id", "") or "")
    session_key = str(bootstrap.get("session_key", "") or "")
    dataset = str(bootstrap.get("dataset", "") or get_dataset(config))
    agent_session_name = str(bootstrap.get("agent_session_name", "") or session_id)
    if session_key:
        os.environ["COGNEE_SESSION_KEY"] = session_key
    if session_id:
        os.environ["COGNEE_SESSION_ID"] = session_id
    service_url = _with_scheme(bootstrap.get("base_url", "") or _LOCAL_SERVICE_URL)
    health_url = _health_url(service_url)
    config["base_url"] = service_url
    os.environ["COGNEE_BASE_URL"] = service_url

    # The hook began ``setting_up`` and handed the verdict to this worker. A
    # boot (an install on a cold start) runs for minutes, past the stale window
    # the status surfaces give an active phase; a heartbeat keeps the phase
    # fresh until the session is registered, so the row keeps saying "setting
    # up memory" instead of falling back to an old result.
    heartbeat_stop = threading.Event()

    def _heartbeat() -> None:
        while not heartbeat_stop.wait(_SETTING_UP_HEARTBEAT_SECONDS):
            phase_touch("setting_up", session_key=session_key)

    heartbeat = threading.Thread(target=_heartbeat, name="cognee-setting-up", daemon=True)
    heartbeat.start()

    def _stop_heartbeat() -> None:
        # ``set()`` wakes the thread but does not interrupt a touch already
        # past its read: that touch would write ``active: setting_up`` back
        # over the result ``phase_end`` records next, and the row would say
        # "setting up memory" until the phase went stale. The thread leaves
        # its wait at once, so the join returns as soon as any touch lands.
        heartbeat_stop.set()
        heartbeat.join(timeout=1.0)

    def _registered() -> None:
        # Memory is usable from here: the code-graph pass that follows is
        # best-effort and must not hold the verdict back.
        _stop_heartbeat()
        hook_log("bootstrap_registered", {"session_id": session_id})
        phase_end("setting_up", session_key=session_key)

    try:
        # 1. Ensure the server is up. Only the single-flight winner spawns uvicorn;
        #    everyone else waits for /health (the winner may still be migrating).
        if not _health_ok(health_url):
            with _bootstrap_singleflight() as acquired:
                if acquired:
                    try:
                        _ensure_local_server_running(
                            config, health_timeout=_SERVER_BOOT_DEADLINE_SECONDS
                        )
                    except Exception as exc:
                        hook_log("server_bootstrap_warning", {"error": str(exc)[:200]})
                else:
                    hook_log("bootstrap_waiting_for_peer", {"session_id": session_id})
            if not _wait_for_health(_SERVER_BOOT_DEADLINE_SECONDS, health_url):
                hook_log("bootstrap_server_unhealthy", {"session_id": session_id})
                _stop_heartbeat()
                phase_end("setting_up", session_key=session_key, ok=False)
                return

        # 2. Register this agent/session. Runs for every agent — NOT gated by the
        #    boot single-flight. _run_heavy's _ensure_local_server_running is a
        #    no-op now that the server is healthy.
        try:
            _, _, registered = await _run_heavy(
                config,
                cwd,
                session_id,
                agent_session_name,
                session_key,
                dataset,
                managed_endpoint=False,
                boot_timeout=_SERVER_BOOT_DEADLINE_SECONDS,
                on_registered=_registered,
            )
            hook_log(
                "bootstrap_complete", {"session_id": session_id, "registered": bool(registered)}
            )
            if not heartbeat_stop.is_set():
                if registered:
                    # The heavy path came back ready without calling back.
                    _registered()
                else:
                    # A refused registration is a False return, not an
                    # exception: _run_heavy reports it and records the
                    # connection state itself. Nothing is registered, so
                    # memory is not ready.
                    hook_log("bootstrap_registration_failed", {"session_id": session_id})
                    _stop_heartbeat()
                    phase_end("setting_up", session_key=session_key, ok=False)
        except Exception as exc:
            hook_log("bootstrap_failed", {"error": str(exc)[:300]})
            _stop_heartbeat()
            phase_end("setting_up", session_key=session_key, ok=False)
    finally:
        _stop_heartbeat()


def _running_server_observer(target_url: str) -> tuple[bool, bool | None]:
    """(a local server is already up, whether it was spawned with the observer).

    The second value is None when that is unknown: a server booted by another
    integration or an older build, or one this plugin did not spawn at all.
    """
    if not target_url or not _is_local_url(target_url):
        return False, None
    try:
        from _plugin_common import live_server_record

        port = urllib.parse.urlparse(target_url).port or 80
        record = live_server_record(port)
    except Exception:
        record = {}
    if record:
        stamp = record.get("llm_observer")
        return True, (bool(stamp) if isinstance(stamp, bool) else None)
    return _health_ok(_health_url(target_url), timeout=1.0), None


def _apply_observer(config: dict, target_url: str) -> dict:
    """Decide the Claude observer for this launch, apply its env, start the shim.

    Returns the decision (``_observer.resolve_observer``) extended with
    ``shim_running``, ``server_running`` and ``server_observer``. Never raises:
    the observer is a convenience over the regular local mode, and a failure
    here must leave that mode intact — except under ``COGNEE_LLM_OBSERVER=true``,
    whose refusal is recorded so the user learns why the server has no LLM.

    The environment only reaches a server this launch spawns. When one is
    already running, its own boot-time config is what serves cognify, so that —
    not this launch's decision — sets ``active``: a server booted on the
    observer keeps needing the shim even after the user adds a key, and one
    booted with a key ignores the observer until it restarts.
    """
    try:
        from _observer import apply_observer_env, ensure_observer_running, resolve_observer

        decision = resolve_observer(config)
        server_running, server_observer = _running_server_observer(target_url)
        decision["wanted"] = bool(decision.get("active"))
        decision["server_running"] = server_running
        decision["server_observer"] = server_observer
        if server_running and server_observer is not None:
            decision["active"] = server_observer
        if decision.get("error"):
            hook_log("observer_refused", {"error": decision["error"]})
        if not decision.get("active"):
            hook_log(
                "observer_skipped",
                {
                    "reason": decision.get("reason", ""),
                    "setting": decision.get("setting", ""),
                    "server_running": server_running,
                    "server_observer": server_observer,
                },
            )
            decision["shim_running"] = False
            return decision
        if not decision.get("claude"):
            # A running observer server this launch would not have chosen (the
            # CLI is gone): nothing to start, but say what the server expects.
            decision["shim_running"] = False
            return decision
        applied = apply_observer_env(decision)
        running = ensure_observer_running(decision, cognee_url=target_url, wait=2.0)
        decision["shim_running"] = running
        hook_log(
            "observer_applied",
            {
                "claude": decision.get("claude", ""),
                "model": decision.get("model", ""),
                "model_warning": decision.get("model_warning", ""),
                "endpoint": decision.get("endpoint", ""),
                "embedding": os.environ.get("EMBEDDING_PROVIDER", ""),
                "applied": sorted(applied),
                "shim_running": running,
                "server_running": server_running,
                "server_observer": server_observer,
            },
        )
        if not running:
            hook_log("observer_shim_start_failed", {"endpoint": decision.get("endpoint", "")})
        return decision
    except Exception as exc:
        hook_log("observer_error", {"error": str(exc)[:200]})
        return {"active": False, "reason": "error", "shim_running": False}


def _record_observer(decision: dict) -> None:
    """Note the observer on this launch's record (read by the status line + doctor)."""
    try:
        from _plugin_common import _read_map_record, _write_map_record, get_session_key

        host_key = get_session_key()
        if not host_key:
            return
        record = _read_map_record(host_key)
        if not record:
            return
        record["llm_observer"] = {
            "active": bool(decision.get("active")),
            "model": str(decision.get("model") or ""),
            "endpoint": str(decision.get("endpoint") or ""),
        }
        _write_map_record(host_key, record)
    except Exception as exc:
        hook_log("observer_record_failed", {"error": str(exc)[:200]})


def _observer_note(observer: dict) -> str:
    """The SessionStart systemMessage line about the observer, or ''."""
    from _observer import SPEND_WARNING, embedding_warning

    active = bool(observer.get("active"))
    wanted = bool(observer.get("wanted", active))
    server_running = bool(observer.get("server_running"))
    server_observer = observer.get("server_observer")
    restart = (
        "It keeps that config until it restarts (it stops on its own once every "
        "session using it has closed)."
    )
    if wanted and server_running and server_observer is None:
        # Nothing records how this server booted (another integration, an older
        # build, or a server started outside the plugin). The env this launch
        # applied does not reach it, so do not claim the observer serves it.
        return (
            "Cognee Memory: a Cognee server was already running and it is unknown which "
            "LLM it was started with, so the Claude observer may not be serving it. If it "
            f"is, cognify/improve use your Claude subscription. {restart}"
        )
    if active:
        lines = [
            f"⚠ LLM: Claude Code (observer), model {observer.get('model')}. {SPEND_WARNING}",
            embedding_warning(),
        ]
        if observer.get("model_warning"):
            lines.append(f"Warning: {observer['model_warning']}")
        if server_running and server_observer is True and not wanted:
            lines.append(
                "The running Cognee server was started on the observer, so it still uses "
                f"your subscription although this session would not have chosen it. {restart}"
            )
        else:
            lines.append(
                "To use a provider key instead, set the plugin's LLM API key option; "
                "COGNEE_LLM_OBSERVER=false turns the observer off."
            )
        if not observer.get("shim_running"):
            lines.append(
                "Warning: the observer shim is not running — check "
                "~/.cognee-plugin/observer/observer.log; the server has no LLM until it runs."
            )
        return "\n".join(lines)
    if wanted and server_running and server_observer is False:
        return (
            "Cognee Memory: the Claude observer was not applied — the running Cognee "
            f"server was started with its own LLM config. {restart}"
        )
    if observer.get("error"):
        return f"Cognee Memory: {observer['error']}"
    return ""


def _with_observer_note(output: dict, observer: dict) -> dict:
    """Tell the user (systemMessage) when the server's LLM is the Claude subscription,
    what that costs, or why a requested observer could not be honoured."""
    try:
        note = _observer_note(observer)
    except Exception as exc:
        hook_log("observer_note_failed", {"error": str(exc)[:200]})
        note = ""
    if not note:
        return output
    result = dict(output or {})
    hso = dict(result.get("hookSpecificOutput") or {})
    hso.setdefault("hookEventName", "SessionStart")
    existing = str(hso.get("systemMessage") or "").strip()
    hso["systemMessage"] = f"{existing}\n\n{note}" if existing else note
    result["hookSpecificOutput"] = hso
    # Claude Code shows only the top-level ``systemMessage`` to the user; the
    # nested one is kept for readers of hookSpecificOutput (Antigravity adapter).
    top = str(result.get("systemMessage") or "").strip()
    result["systemMessage"] = f"{top}\n\n{note}" if top else note
    return result


def _mode_word(config: dict) -> str:
    """The mode the user is told about, decided the way the status line decides
    it: a forced backend first, then whether the target host is loopback.

    By the time the message is built, ``config["base_url"]`` holds the target
    the bootstrap filled in, which for a local session is the localhost default,
    so "is a URL set" cannot tell local from cloud (it said cloud for every
    local session, forced-local included).
    """
    forced = str(config.get("_forced_backend") or "")
    if forced == "local":
        return "local"
    url = str(config.get("base_url") or "").strip()
    if not url:
        return "cloud" if forced == "cloud" else "local"
    return "local" if _is_loopback_host(urllib.parse.urlparse(url).hostname or "") else "cloud"


def _session_start_guidance(mode: str, dataset: str, session_id: str, ready: bool) -> dict:
    if ready:
        message = (
            "## Cognee Memory Connected\n"
            f"Mode: {mode} | Dataset: {dataset} | Session: {session_id}\n\n"
            "Cognee organizes knowledge into three categories.\n"
            "- user_context: user preferences and personal facts\n"
            "- project_docs: repository and project knowledge\n"
            "- agent_actions: tool traces and agent findings\n\n"
            "Use /cognee-memory:cognee-remember to store permanent memory, "
            "and /cognee-memory:cognee-search to query by node set."
        )
    else:
        message = (
            "## Cognee Memory Connecting\n"
            f"Mode: {mode} | Dataset: {dataset} | Session: {session_id}\n\n"
            "The local Cognee server is starting up (first run or database "
            "migrations can take a little while). Your prompts work normally now; "
            "memory recall activates automatically once the server is ready."
        )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "systemMessage": message,
        }
    }


_MEMORY_PREFERENCE_STEER = (
    "Use cognee-search for memory and cognee-remember to save it. "
    "Before searching, say 'Searching Cognee…' in progress text. "
    "Briefly credit recalled memory only when used."
)


def _apply_memory_preference(output: dict) -> dict:
    """Steer Claude to treat Cognee as the preferred memory over native auto-memory.

    Claude Code's built-in auto memory (MEMORY.md) can't be reliably disabled by a
    plugin, so instead we assert Cognee as the authoritative memory via a SessionStart
    ``additionalContext`` instruction (injected into the model's context, not shown to
    the user). Applied across all SessionStart branches. Opt out with
    ``COGNEE_PREFER_MEMORY=false``.
    """
    try:
        val = load_config().get("prefer_cognee_memory", True)
    except Exception:
        val = True
    if str(val).strip().lower() in {"0", "false", "no", "off"}:
        return output

    result = dict(output or {})
    hso = dict(result.get("hookSpecificOutput") or {})
    hso.setdefault("hookEventName", "SessionStart")
    existing = str(hso.get("additionalContext") or "").strip()
    hso["additionalContext"] = (
        f"{existing}\n\n{_MEMORY_PREFERENCE_STEER}" if existing else _MEMORY_PREFERENCE_STEER
    )
    result["hookSpecificOutput"] = hso
    return result


async def _start(payload: dict | None = None) -> dict:
    """Session start (:func:`_start_session`).

    Once project memory began, the session is this hook's to prepare until a
    worker takes it over (the cloud worker, or a bootstrap that did spawn).
    However the hook ends without that (memory offline, a failed registration,
    an exception), a session ``prepare()`` never ran for is handed to a later
    ``route()``; a prepared or backed-off state is left alone.
    """
    setup: dict = {}
    try:
        return await _start_session(payload, setup)
    finally:
        if setup.get("project_memory"):
            _project_memory_gave_up(*setup["project_memory"])


async def _start_session(payload: dict | None, setup: dict) -> dict:
    config = load_config()
    payload = payload or {}
    cwd = str(payload.get("cwd") or os.environ.get("CLAUDE_CWD") or os.getcwd())
    # The service URL is the sole router (api_key is optional auth, with a
    # default-user fallback in registration). COGNEE_AGENT_MODE is NOT decided
    # here: it's set only when we actually boot a server (in
    # _ensure_local_server_running), so connecting to an already-running server
    # never claims ownership of its teardown.
    configured_url = _with_scheme(str(config.get("base_url", "") or "").strip())
    # Forced cloud (backend switch) with no URL configured: there is nothing to
    # boot and nothing to fall back to — keep the target empty so no local
    # server is spawned, let the connection attempt fail, and let the status
    # line report the missing URL. Everything else keeps the localhost default.
    forced_cloud_unconfigured = not configured_url and config.get("_forced_backend") == "cloud"
    target_url = configured_url or ("" if forced_cloud_unconfigured else _LOCAL_SERVICE_URL)
    config["base_url"] = target_url
    os.environ["COGNEE_BASE_URL"] = target_url

    # Claude observer: local mode with no LLM key of its own runs the server's
    # LLM calls through Claude Code (`claude -p --safe-mode`) behind a loopback
    # OpenAI-compatible shim, with embeddings on fastembed. Decided and applied
    # HERE, before the install/boot below: the install reads EMBEDDING_PROVIDER
    # to add the fastembed extra, and the server inherits the provider variables
    # at spawn. The shim is started detached now so the server's first LLM call
    # (and the watcher's key check) find it listening.
    from _cloud_worker import enabled as cloud_enabled

    cloud = cloud_enabled({"base_url": target_url})
    observer = (
        {"active": False, "reason": "cloud"} if cloud else _apply_observer(config, target_url)
    )

    # NOTE: the local server's LLM_API_KEY health is deliberately NOT judged here.
    # A hook-env read (config's llm_api_key / OPENAI_API_KEY) is blind to a key that
    # lives in cognee's own config or a .env the server loads, so a session launched
    # from a shell without the export wrote "not_set" into a marker that EVERY
    # session's status line reads — a false ✕ (incorrect_llm_api_key) for sessions whose key is
    # fine. The idle watcher is the sole authority instead: it resolves the key the
    # same way the server does (cognee's get_llm_config) and validates it against
    # the provider, so the verdict matches reality. It runs at session start, so the
    # signal still appears within seconds of launch.

    # The host (Claude) session id is a local correlation key only: it keeps every
    # hook process of this launch resolving the SAME Cognee session id (via the
    # host-keyed map). It is never sent to Cognee as an identity.
    session_candidate, session_source = resolve_session_key_from_payload(payload)
    session_key = set_session_key(session_candidate)
    if not session_key:
        # The mid-bootstrap venv re-exec (_run_heavy) replays this script after
        # stdin was already consumed, so the payload is gone — but the pre-exec
        # run exported COGNEE_SESSION_KEY, which execv preserves.
        session_key = get_session_key()
        if session_key:
            session_source = "env_session_key"
    if not session_key:
        hook_log("missing_payload_session_id", {"cwd": cwd, "payload": payload})
        print(
            "cognee-plugin: missing payload session_id; refusing to register",
            file=sys.stderr,
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "systemMessage": "Cognee Memory: session key missing in SessionStart payload.",
            }
        }
    os.environ["COGNEE_SESSION_KEY"] = session_key
    if payload.get("source") in {"clear", "compact"}:
        from _injection_ledger import reset as reset_injection_ledger
        from _injection_ledger import session_dir as ledger_dir

        try:
            reset_injection_ledger(session_key)
        except Exception as exc:
            # Never block startup on the ledger. A stale ledger would claim the
            # forgotten context is still present, so drop it instead.
            hook_log("injection_ledger_failed", {"op": "reset", "error": str(exc)[:200]})
            try:
                (ledger_dir(session_key) / "injected.json").unlink()
            except (OSError, ValueError):
                pass

    # Resolve (and persist) this launch's record: session_id (data scoping, unique
    # per launch) + conn_uuid (liveness handle for registration/counting). Written
    # synchronously here so prompt hooks read back the identical ids before any run.
    # The record also seeds the launch's active dataset (env/default) and stores
    # the host pid + cwd so switch-dataset.py, which runs under the shell tool
    # with no hook payload, can find this launch. A resumed record keeps the
    # dataset it already has — including one chosen by a switch.
    session_id, conn_uuid = ensure_launch_record(
        session_key,
        cwd,
        dataset=str(config.get("dataset", "") or "").strip(),
        host_pid=_find_claude_parent_pid(),
    )
    # The server this launch registers on. Hooks read the URL option again, so a
    # later change is detected against this and reported (base_url_change_notice).
    set_launch_base_url(session_key, target_url)
    # The launch record is what the status line and doctor read — neither has
    # this process's environment — so the observer decision lands on it here,
    # once the record exists.
    _record_observer(observer)
    _record_launch_facts(config, target_url, observer)
    from _project_memory import begin as begin_project_memory

    begin_project_memory(get_dataset(config), session_id, cwd)
    setup["project_memory"] = (get_dataset(config), session_id)
    os.environ["COGNEE_SESSION_ID"] = session_id
    agent_session_name = conn_uuid
    dataset = get_dataset(config)
    # Registration and the bootstrap worker read the dataset off config: keep it
    # in step with the record so a resumed, switched launch re-binds correctly.
    config["dataset"] = dataset
    hook_log(
        "session_resolved",
        {
            "source": session_source,
            "session_key": session_key,
            "session_id": session_id,
            "conn_uuid": conn_uuid,
            "dataset": dataset,
        },
    )

    if cloud:
        from _cloud_worker import register_session

        register_session(
            {
                "cwd": cwd,
                "session_id": session_id,
                "session_key": session_key,
                "dataset": dataset,
                "agent_session_name": agent_session_name,
                "base_url": target_url,
            }
        )
        # The cloud worker retries registration and prepares on success.
        setup.pop("project_memory", None)
        touch_activity()
        marker_ready = server_ready_hint(target_url)
        message = (
            "Cognee cloud memory ready; background synchronization active."
            if marker_ready
            else "Cognee cloud memory connecting in the background; captures are queued."
        )
        # A remote URL or managed endpoint the plugin did not start: its coding
        # mode is unknown (SDK-1336). Cognee Cloud is excluded inside.
        cloud_coding_note = _coding_server_note(
            target_url,
            False,
            managed=managed_endpoint_enabled(config),
            cloud_backend=config.get("_forced_backend") == "cloud",
        )
        return _with_system_note(
            _with_system_note(
                {
                    "systemMessage": message,
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "systemMessage": message,
                    },
                },
                cloud_coding_note,
            ),
            _project_memory_note(dataset, session_id),
        )

    # Boot-vs-connect is decided purely by whether the server is already up:
    #   * up                -> connect (we don't boot, so agent mode is left as-is)
    #   * down + local URL  -> boot it; agent mode is set at the uvicorn spawn so
    #                          the server tears down once all agents disconnect
    #   * down + remote URL -> can't boot a remote host; connect and degrade
    user_id = ""
    agent_api_key = ""
    # An empty target (forced cloud, no URL) must never boot: _is_local_url("")
    # parses to localhost, so gate on the URL being present at all.
    # Local endpoints get the full-evidence presence check (quick form — the
    # boot path re-verifies rigorously before any install), so the recorded
    # decision distinguishes a busy server from an absent one. Remote endpoints
    # keep the plain HTTP probe: there is no local evidence for them, and they
    # are never booted. will_boot means "enter the local bootstrap path" — the
    # actual install/boot license is enforced inside _ensure_local_server_running,
    # so a busy server still gets a worker that waits for it instead of a boot.
    phase_begin("connecting", session_key=session_key)
    presence_verdict, presence_evidence = "", {}
    if target_url and _is_local_url(target_url):
        presence_verdict, presence_evidence = server_presence(target_url, confirm_absent=False)
        server_live = presence_verdict == PRESENCE_READY
    else:
        server_live = bool(target_url) and _health_ok(_health_url(target_url))
    # COGNEE_MANAGED_ENDPOINT: the URL belongs to a deployment we don't own, so
    # it is never booted. This is stronger than _run_heavy's
    # ``managed_endpoint=not will_boot`` (connect-only for THIS call): the lock
    # also stops the out-of-band retry and _ensure_local_server_running itself.
    managed_locked = managed_endpoint_enabled(config)
    will_boot = (
        (not server_live) and bool(target_url) and _is_local_url(target_url) and not managed_locked
    )
    hook_log(
        "endpoint_mode_selected",
        {
            "base_url": target_url,
            "server_live": server_live,
            "will_boot": will_boot,
            "managed_endpoint": managed_locked,
            **(
                {"presence": presence_verdict, "evidence": presence_evidence}
                if presence_verdict
                else {}
            ),
        },
    )
    # A shared local server keeps the distillation mode it booted with; say so
    # when it differs from this session's coding memory setting (SDK-1336).
    coding_note = _coding_server_note(
        target_url,
        server_live,
        managed=managed_locked,
        cloud_backend=config.get("_forced_backend") == "cloud",
    )
    if managed_locked and target_url and not server_live:
        # Managed deployment is down: fail loudly, never fork a fallback brain.
        hook_log("managed_endpoint_down", {"base_url": target_url})
        print(
            f"cognee-plugin: managed endpoint {target_url} unreachable — memory OFFLINE",
            file=sys.stderr,
        )
        # The status line and the recall gate read the shared marker, not this
        # output: without this they would keep the previous session's verdict.
        write_connection_state("unreachable", target_url)
        phase_end("connecting", session_key=session_key, ok=False)
        offline_message = (
            "## ⚠ Cognee Memory OFFLINE\n"
            f"The managed Cognee endpoint {target_url} is unreachable. "
            "COGNEE_MANAGED_ENDPOINT is set, so no local fallback server was "
            "started — memory recall and capture are disabled for this session.\n"
            "Start the deployment, then start a new session (or /clear)."
        )
        return {
            # Top level is where the universal ``systemMessage`` is documented
            # and where the Antigravity adapter reads it; keep both copies.
            "systemMessage": offline_message,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "systemMessage": offline_message,
                "additionalContext": (
                    "Cognee memory is OFFLINE for this session: the managed endpoint "
                    f"{target_url} is unreachable and local fallback is disabled by "
                    "COGNEE_MANAGED_ENDPOINT. Nothing is being recalled or captured. "
                    "If durable memory matters for the current task, remind the user "
                    "that the Cognee stack is down before proceeding."
                ),
            },
        }
    # Whether a detached bootstrap worker owns the "memory ready" verdict: it
    # ends ``setting_up`` once the server answers and the session is
    # registered. This hook then records no verdict of its own: the server is
    # still booting here, and an ``ok=False`` written now would outlive the
    # phase and read as "memory unavailable" next to a live server.
    worker_owns_ready = False
    if will_boot and _LAZY_BOOTSTRAP:
        # The status surfaces show the phase meanwhile.
        phase_begin("setting_up", "starting memory server", session_key=session_key)
        if _spawn_bootstrap(config, cwd, session_id, agent_session_name, session_key, dataset):
            setup.pop("project_memory", None)
        worker_owns_ready = True
        user_id = os.environ.get("COGNEE_USER_ID", "")
    else:
        user_id, agent_api_key, ok = await _run_heavy(
            config,
            cwd,
            session_id,
            agent_session_name,
            session_key,
            dataset,
            managed_endpoint=not will_boot,
            boot_timeout=_HEALTH_TIMEOUT_SECONDS,
        )
        if not ok:
            if _LAZY_BOOTSTRAP and target_url and _is_local_url(target_url) and not managed_locked:
                # Inline attempt failed; retry the heavy path out of band.
                phase_begin("setting_up", "starting memory server", session_key=session_key)
                if _spawn_bootstrap(
                    config, cwd, session_id, agent_session_name, session_key, dataset
                ):
                    setup.pop("project_memory", None)
                worker_owns_ready = True
            else:
                phase_end("connecting", session_key=session_key, ok=False)
                return {}

    # Remove legacy resolved cache files. Runtime state now comes from HTTP endpoints.
    _purge_legacy_resolved_files()
    # Per-session files nothing else deletes (launch records, status markers,
    # bridge caches, pending buffers, dead improve locks) and oversized logs.
    from _plugin_common import sweep_stale_state

    sweep_stale_state()

    # Reset the idle clock for this Claude process before the watcher
    # starts, otherwise a stale timestamp from a prior session can cause
    # an immediate improve on startup.
    touch_activity()

    # Launch the idle watcher (update and LLM-key checks, periodic shared-memory
    # refresh). COGNEE_IDLE_DISABLED opts out.
    if os.environ.get("COGNEE_IDLE_DISABLED", "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        _spawn_idle_watcher(session_id, dataset, user_id, config, session_key)

    _spawn_exit_watcher(
        session_id,
        dataset,
        session_key=session_key,
        agent_session_name=agent_session_name,
        api_key=agent_api_key,
        service_url=str(config.get("base_url", "") or ""),
    )

    mode = _mode_word(config)
    print(
        f"cognee-plugin: session ready (mode={mode}, "
        f"session={session_id}, dataset={dataset}, user={user_id[:8]}...)",
        file=sys.stderr,
    )

    ready = server_live or server_ready_hint(str(config.get("base_url", "") or ""))
    if worker_owns_ready:
        hook_log("bootstrap_owns_ready", {"session_id": session_id, "ready_now": bool(ready)})
    else:
        phase_end("connecting", session_key=session_key, ok=bool(ready))
    return _with_system_note(
        _with_system_note(
            _with_observer_note(
                _session_start_guidance(mode, dataset, session_id, ready), observer
            ),
            coding_note,
        ),
        _project_memory_note(dataset, session_id),
    )


def _coding_server_note(
    target_url: str, server_live: bool, *, managed: bool, cloud_backend: bool = False
) -> str:
    """The note when the server's distillation mode differs from, or may differ
    from, this session's coding memory setting (SDK-1336), or ''.

    A live local server the plugin spawned is read from its pidfile; one with no
    pidfile record, a remote URL or a managed endpoint was not started by the
    plugin, so a coding session is told to shut it down and restart the plugin.
    Cognee Cloud (the cloud backend, ``*.cognee.ai``) is never asked. Never
    restarts the server.
    """
    if not target_url:
        return ""
    local = _is_local_url(target_url)
    try:
        from _plugin_common import (
            coding_server_note,
            coding_unowned_server_note,
            is_cognee_cloud_url,
        )

        coding = coding_memory_enabled()
        if local and not managed:
            if not server_live:
                return ""
            port = urllib.parse.urlparse(target_url).port or 80
            note = coding_server_note(port, coding)
        elif not coding or cloud_backend or is_cognee_cloud_url(target_url):
            return ""
        else:
            note = coding_unowned_server_note(target_url, remote=not local, managed=managed)
    except Exception as exc:
        hook_log("coding_server_note_failed", {"error": str(exc)[:200]})
        return ""
    if note:
        hook_log(
            "coding_server_mode_mismatch",
            {"base_url": target_url, "managed": managed, "coding": coding_memory_enabled()},
        )
    return note


def _project_memory_gave_up(dataset: str, session_id: str) -> None:
    """Session setup ended before prepare() and nothing retries it: let a later
    route() prepare the session instead of holding its memory for good."""
    try:
        from _project_memory import allow_retry

        allow_retry(dataset, session_id)
    except Exception as exc:
        hook_log("project_memory_retry_mark_failed", {"error": str(exc)[:200]})


def _project_memory_note(dataset: str, session_id: str) -> str:
    """The one-time note while this session's memory waits on the project tag check."""
    try:
        from _project_memory import pending_notice

        return pending_notice(dataset, session_id)
    except Exception:
        # Best effort: the note never blocks startup.
        return ""


def _with_system_note(output: dict, note: str) -> dict:
    """Append ``note`` to the SessionStart systemMessage (nested and top level)."""
    if not note:
        return output
    result = dict(output or {})
    hso = dict(result.get("hookSpecificOutput") or {})
    hso.setdefault("hookEventName", "SessionStart")
    existing = str(hso.get("systemMessage") or "").strip()
    hso["systemMessage"] = f"{existing}\n\n{note}" if existing else note
    result["hookSpecificOutput"] = hso
    top = str(result.get("systemMessage") or "").strip()
    result["systemMessage"] = f"{top}\n\n{note}" if top else note
    return result


# The one-time notice for settings ~/.cognee/.env still holds but the plugin
# no longer reads. The flag lives in the plugin's own state, never in Claude
# Code's settings.
_ENV_UPGRADE_NOTICE_FLAG = _STATE_DIR / "env-upgrade-notice.json"
ENV_UPGRADE_NOTICE = (
    "Cognee for Claude Code no longer reads Cloud or LLM settings from ~/.cognee/.env. "
    "Set your Cognee Cloud URL and API key, or your LLM settings for local mode, in the "
    "plugin's options (/plugin → cognee-memory). Until then Cognee runs locally with "
    "defaults."
)
# Its own one-time notice: storage settings the plugin's local server no longer
# takes from ~/.cognee/.env or the shell.
_STORAGE_NOTICE_FLAG = _STATE_DIR / "env-storage-notice.json"
STORAGE_NOTICE = (
    "Cognee for Claude Code no longer reads storage settings from ~/.cognee/.env or "
    "the shell. Its local server uses the built-in stores under ~/.cognee; data stored "
    "elsewhere is untouched but not used."
)
CLOUD_KEY_EMPTY_NOTICE = (
    "Cognee Memory: the Cloud API key option is empty, so Cognee Cloud will refuse "
    "this session. Set it in the plugin's options (/plugin → cognee-memory)."
)
CLOUD_PIN_NO_URL_NOTICE = (
    "Cognee Memory: COGNEE_BACKEND=cloud is set but the Cognee Cloud URL option is "
    "empty, so Cognee sends nothing. Set the URL in the plugin's options (/plugin → "
    "cognee-memory), or remove the switch to run locally."
)
CLOUD_URL_EMPTY_NOTICE = (
    "The Cognee Cloud API key option is set but the Cloud URL is empty, so Cognee is "
    "running locally; set the URL to use Cloud."
)


def _option_notices() -> list:
    """The SessionStart notes about the plugin options. Never raises.

    Once ever: ~/.cognee/.env holds Cloud or LLM settings and the matching
    options are empty. Once ever, separately: storage settings (data
    directories, database providers) in the file or the shell, which the
    plugin's local server no longer uses. Every session: a Cloud URL without its key, or a key
    without a URL. Reads only option presence and setting names, never values.
    """
    notes: list = []
    try:
        from _env_file import (
            cloud_pinned_without_url,
            legacy_settings_in_file,
            option,
            storage_settings_ignored,
            url_option,
        )

        legacy = legacy_settings_in_file()
        if (legacy.get("cloud") or legacy.get("llm")) and not _ENV_UPGRADE_NOTICE_FLAG.exists():
            notes.append(ENV_UPGRADE_NOTICE)
            try:
                _ENV_UPGRADE_NOTICE_FLAG.parent.mkdir(parents=True, exist_ok=True)
                _ENV_UPGRADE_NOTICE_FLAG.write_text(
                    json.dumps(
                        {
                            "shown_at": time.time(),
                            "names": sorted(legacy.get("cloud", []) + legacy.get("llm", [])),
                        }
                    ),
                    encoding="utf-8",
                )
            except OSError as exc:
                hook_log("env_upgrade_notice_flag_failed", {"error": str(exc)[:200]})
        storage = storage_settings_ignored()
        if storage and not _STORAGE_NOTICE_FLAG.exists():
            notes.append(STORAGE_NOTICE)
            try:
                _STORAGE_NOTICE_FLAG.parent.mkdir(parents=True, exist_ok=True)
                _STORAGE_NOTICE_FLAG.write_text(
                    json.dumps({"shown_at": time.time(), "names": storage}), encoding="utf-8"
                )
            except OSError as exc:
                hook_log("env_storage_notice_flag_failed", {"error": str(exc)[:200]})
        if _HIDES_KEYS_MARKER.exists():
            notes.append(SERVER_HIDES_KEYS_MESSAGE)
        url = url_option()
        key_set = bool(option("cognee_api_key"))
        if url and not service_url_is_local(url) and not key_set:
            notes.append(CLOUD_KEY_EMPTY_NOTICE)
        if cloud_pinned_without_url():
            notes.append(CLOUD_PIN_NO_URL_NOTICE)
        elif key_set and not url:
            notes.append(CLOUD_URL_EMPTY_NOTICE)
    except Exception as exc:
        hook_log("option_notices_failed", {"error": str(exc)[:200]})
    return notes


def _apply_option_notices(output: dict) -> dict:
    for note in _option_notices():
        output = _with_system_note(output, note)
    return output


def _record_launch_facts(config: dict, target_url: str, observer: dict) -> None:
    """Note what this launch decided, for doctor (which has no options). Never raises.

    Booleans and names only: whether each key option is set, the LLM provider
    and model as applied, the observer decision, and where the key comes from.
    """
    try:
        from _env_file import option
        from _plugin_common import get_session_key, record_launch_facts

        local = not target_url or service_url_is_local(target_url)
        record_launch_facts(
            get_session_key(),
            {
                "cloud_key_option_set": bool(option("cognee_api_key")),
                "llm_key_option_set": bool(option("llm_api_key")),
                "embedding_key_option_set": bool(option("embedding_api_key")),
                "llm_provider": "" if not local else os.environ.get("LLM_PROVIDER", ""),
                "llm_model": "" if not local else os.environ.get("LLM_MODEL", ""),
                "observer": {
                    "active": bool(observer.get("active")),
                    "reason": str(observer.get("reason") or ""),
                    "model": str(observer.get("model") or ""),
                },
                "key_source": (
                    "local_owner_key"
                    if local
                    else ("plugin_option" if option("cognee_api_key") else "missing")
                ),
            },
        )
    except Exception as exc:
        hook_log("launch_facts_failed", {"error": str(exc)[:200]})


def _apply_sync_notice(output: dict, payload: dict | None = None) -> dict:
    """Say, on reopening a chat, whether it reached long-term memory (SDK-1071).

    The session-end sync runs after the chat is gone, so its outcome is shown
    when the same chat is resumed: resume keeps the session id, and the
    session's improve record says when it was last sent or why the send
    failed. Shown once per sync, in the terminal and to the agent, and only on
    resume: a compaction keeps the session too, but lands mid-task, where an
    idle or auto sync's notice would only interrupt. Off with
    ``COGNEE_PLUGIN_CITE_MEMORY=off``.
    """
    if str((payload or {}).get("source") or "") != "resume":
        return output
    try:
        import _plugin_common
        from _recall_text import credit_enabled, mark_notice_shown, notice_due, sync_notice
        from config import get_session_id

        if not credit_enabled(_plugin_common.CITE_MEMORY_DEFAULT):
            return output
        session_id = get_session_id(load_config())
        message, sync_ts = sync_notice(_plugin_common.read_improve_state(session_id))
        if not message or not notice_due(_plugin_common._PLUGIN_DIR, session_id, sync_ts):
            return output
        mark_notice_shown(_plugin_common._PLUGIN_DIR, session_id, sync_ts)
    except Exception as exc:
        hook_log("sync_notice_failed", {"error": str(exc)[:200]})
        return output
    hook_log("sync_notice_shown", {"session": session_id, "sync_ts": sync_ts})

    result = dict(output or {})
    hso = dict(result.get("hookSpecificOutput") or {})
    hso.setdefault("hookEventName", "SessionStart")
    for field in ("systemMessage", "additionalContext"):
        existing = str(hso.get(field) or "").strip()
        hso[field] = f"{existing}\n\n{message}" if existing else message
    result["hookSpecificOutput"] = hso
    # The user sees only the top-level systemMessage: Claude Code documents it
    # there (inside hookSpecificOutput it reached the agent but was never shown).
    top = str(result.get("systemMessage") or "").strip()
    result["systemMessage"] = f"{top}\n\n{message}" if top else message
    return result


#: Said once per machine and dataset (SDK-1393): recall is auto by default, so
#: a configured dataset no longer restricts it.
RECALL_MODE_NOTICE = (
    "Cognee: recall now searches every dataset you can read, not only `{dataset}` "
    "(saves still go there). To keep recall on `{dataset}`, set "
    "`COGNEE_PLUGIN_RECALL_MODE=pinned`, or ask to pin recall for this session. To "
    "search one dataset once, name it in your request."
)


def _apply_recall_mode_notice(output: dict, payload: dict | None = None) -> dict:
    """Tell a user whose configured dataset used to restrict recall that it no longer does.

    Shown once per machine and dataset, in the message the user sees (not the
    agent's context), when ``COGNEE_PLUGIN_DATASET`` names a non-default
    dataset, ``COGNEE_PLUGIN_RECALL_MODE`` was never set (exported or in
    ``~/.cognee/.env``) and this session has not pinned recall itself. Not on a
    compaction, which lands mid-task.
    """
    if str((payload or {}).get("source") or "") == "compact":
        return output
    try:
        import _plugin_common
        from _recall_text import mark_notice_shown, notice_due
        from config import _DEFAULTS

        if os.environ.get("COGNEE_PLUGIN_RECALL_MODE", "").strip():
            return output
        dataset = str(load_config().get("dataset") or "").strip()
        if not dataset or dataset == _DEFAULTS["dataset"]:
            return output
        if not _plugin_common.auto_recall_active(get_session_key()):
            return output
        # The resume notice's once-only marker, keyed by the dataset.
        marker = f"recall-mode:{dataset}"
        if not notice_due(_plugin_common._PLUGIN_DIR, marker, 1.0):
            return output
        mark_notice_shown(_plugin_common._PLUGIN_DIR, marker, 1.0)
    except Exception as exc:
        hook_log("recall_mode_notice_failed", {"error": str(exc)[:200]})
        return output
    hook_log("recall_mode_notice_shown", {"dataset": dataset})
    message = RECALL_MODE_NOTICE.format(dataset=dataset)
    result = dict(output or {})
    hso = dict(result.get("hookSpecificOutput") or {})
    hso.setdefault("hookEventName", "SessionStart")
    existing = str(hso.get("systemMessage") or "").strip()
    hso["systemMessage"] = f"{existing}\n\n{message}" if existing else message
    result["hookSpecificOutput"] = hso
    # The user sees the top-level systemMessage (where Claude Code documents it,
    # and where the Antigravity adapter reads it).
    top = str(result.get("systemMessage") or "").strip()
    result["systemMessage"] = f"{top}\n\n{message}" if top else message
    return result


def main():
    if is_observer_child():
        return
    # Detached bootstrap mode: run the slow server boot + registration out of
    # band so the SessionStart hook itself returns fast.
    if _BOOTSTRAP_ARG in sys.argv:
        idx = sys.argv.index(_BOOTSTRAP_ARG)
        raw = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else ""
        try:
            bootstrap = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            bootstrap = {}
        try:
            asyncio.run(_run_bootstrap(bootstrap))
        except Exception as exc:
            hook_log("bootstrap_main_exception", {"error": str(exc)[:200]})
        return

    payload_raw = sys.stdin.read()
    try:
        payload = json.loads(payload_raw) if payload_raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    output = {}
    try:
        with quiet_hook_output("session-start"):
            output = asyncio.run(_start(payload))
    except Exception as exc:
        hook_log("session_start_exception", {"error": str(exc)[:200]})
    try:
        from _auto_memory import advice as auto_memory_advice
        from _cloud_worker import enabled as cloud_enabled

        # Claude Code's own settings are never written (SDK-1411): at most a
        # one-time note on how to turn its auto-memory off, or back on.
        notice = auto_memory_advice(cloud_enabled())
        if notice:
            hso = output.setdefault("hookSpecificOutput", {"hookEventName": "SessionStart"})
            hso["systemMessage"] = (str(hso.get("systemMessage") or "") + "\n" + notice).strip()
            output["systemMessage"] = hso["systemMessage"]
    except Exception as exc:
        hook_log("auto_memory_setup_failed", {"error": str(exc)[:200]})
    coding = coding_memory_enabled()
    try:
        if payload.get("source") != "resume":
            from _session_stats import previous, summary
            from _session_stats import update as update_stats

            config = load_config()
            key = get_session_key()
            # Coding memory (SDK-1336) records the session too, but shows no recap.
            last_line = (
                ""
                if coding
                else summary(previous(key, get_dataset(config), str(config.get("base_url") or "")))
            )
            update_stats(
                key,
                metadata={
                    "dataset": get_dataset(config),
                    "server_url": str(config.get("base_url") or ""),
                },
            )
            if last_line:
                hso = output.setdefault("hookSpecificOutput", {"hookEventName": "SessionStart"})
                hso["systemMessage"] = (
                    str(hso.get("systemMessage") or "") + "\n" + last_line
                ).strip()
                hso["additionalContext"] = (
                    str(hso.get("additionalContext") or "") + "\n" + last_line
                ).strip()
                output["systemMessage"] = hso["systemMessage"]
    except Exception as exc:
        # The recap is a courtesy; it must never cost the startup output.
        hook_log("session_stats_failed", {"op": "recap", "error": str(exc)[:200]})
    if not coding:
        output = _apply_memory_preference(output)
    if payload.get("source") != "resume" and not coding:
        try:
            from _cloud_worker import enabled as cloud_enabled
            from _context_cache import inject as inject_digest

            digest = (
                inject_digest(
                    get_dataset(load_config()), get_session_key(), source=payload.get("source", "")
                )
                if cloud_enabled()
                else ""
            )
            if digest:
                # Lead with the digest, keeping what is already there (the
                # last-session line, and the steer when the user has not opted out).
                hso = output.setdefault("hookSpecificOutput", {"hookEventName": "SessionStart"})
                existing = str(hso.get("additionalContext") or "").strip()
                hso["additionalContext"] = f"{digest}\n\n{existing}" if existing else digest
        except Exception as exc:
            # A missing, busy or malformed cache must never cost the startup output.
            hook_log("context_digest_inject_failed", {"error": str(exc)[:200]})
    output = _apply_sync_notice(output, payload)
    _flag_missing_local_runtime()
    output = _apply_host_python_warning(output)
    output = _apply_key_limit_warning(output)
    output = _apply_option_notices(output)
    if coding:
        output = _coding_session_start(output, payload)
    else:
        output = _apply_recall_mode_notice(output, payload)
    print(json.dumps(output or {}))


class _SkipAutoindex(Exception):
    """Raised to skip the code-graph auto-index in coding memory mode."""


def _coding_session_start(output: dict, payload: dict) -> dict:
    """Coding memory (SDK-1336) at SessionStart.

    The steer, recap and digest are skipped where they are added; what else is
    already in the output (an OFFLINE instruction, the sync notice) is kept.
    After a compaction the lessons block is appended again (the compaction
    dropped it). Also checks once that the server takes the event times coding
    mode needs, and says so when it does not (memory is then off), and starts
    filling the lesson cache in the background.
    """
    import _coding_memory

    result = dict(output or {})
    hso = dict(result.get("hookSpecificOutput") or {})
    hso.setdefault("hookEventName", "SessionStart")
    key = get_session_key()
    cwd = str(payload.get("cwd") or "") or os.getcwd()
    try:
        with quiet_hook_output("session-start"):
            support = _coding_memory.check_server_support(refresh=True, timeout=3.0)
        if support.get("ok") is False:
            notice = "Cognee coding memory is off: " + str(support.get("reason") or "")
            top = str(result.get("systemMessage") or "").strip()
            result["systemMessage"] = f"{top}\n{notice}".strip()
        elif payload.get("source") == "compact" and key:
            _coding_memory.reset_delivery(key)
            with quiet_hook_output("session-start"):
                block = _coding_memory.deliver(key, _coding_memory.last_request(key), cwd)
            if block:
                existing = str(hso.get("additionalContext") or "").strip()
                hso["additionalContext"] = f"{existing}\n\n{block}" if existing else block
        elif key:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "send_unit", os.path.join(os.path.dirname(__file__), "send-unit.py")
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.spawn_warm(cwd)
    except Exception as exc:
        hook_log("coding_session_start_failed", {"error": str(exc)[:200]})
    result["hookSpecificOutput"] = hso
    return result


if __name__ == "__main__":
    main()
