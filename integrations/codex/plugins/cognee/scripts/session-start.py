#!/usr/bin/env python3
"""Initialize Cognee memory at session start.

Runs on the SessionStart hook. Responsibilities:
  1. Load config (file + env vars)
  2. Compute per-directory session ID
  3. Connect to Cognee Cloud if configured
  4. Configure local LLM if local mode
  5. Register the current Codex thread as an active agent connection
"""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

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
    _https_context,
    _reexec_into_venv,
    apply_cognee_env,
    clear_server_pidfile,
    ensure_launch_record,
    get_session_key,
    hook_log,
    probe_health,
    quiet_hook_output,
    resolve_session_key_from_payload,
    server_presence,
    service_url_is_local,
    set_session_key,
    touch_activity,
    write_connection_state,
    write_server_pidfile,
)
from _proc import find_host_ancestor_windows
from _proc import pid_alive as _pid_alive
from cognee_statusline_render import render_status_for_host
from config import (
    _cloud_http_request,
    _user_id_via_api,
    ensure_cognee_ready,
    ensure_dataset_ready,
    ensure_dataset_ready_via_api,
    ensure_identity,
    get_dataset,
    is_cloud_mode,
    load_config,
)

_STATE_DIR = Path.home() / ".cognee-plugin" / "codex"
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
_LAZY_BOOTSTRAP = os.environ.get("COGNEE_LAZY_BOOTSTRAP", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

# --- Self-managed cognee install (SHARED with the Claude Code plugin) --------
# uv + a uv-managed Python guarantee a cognee-compatible runtime (3.10-3.14)
# regardless of what's on the machine. All paths sit under the shared
# ~/.cognee-plugin root so the two plugins install once and reuse one venv.
_UV_DIR = _GLOBAL_STATE_DIR / "uv"
_UV_BIN = _UV_DIR / ("uv.exe" if os.name == "nt" else "uv")
_UV_PYTHON_DIR = _GLOBAL_STATE_DIR / "python"
_UV_INSTALL_URL = "https://astral.sh/uv/install.sh"
_PINNED_PYTHON = os.environ.get("COGNEE_PLUGIN_PYTHON", "") or "3.12"
_PINNED_COGNEE_VERSION = "1.5.4"
_INSTALL_TIMEOUT_SECONDS = float(os.environ.get("COGNEE_INSTALL_TIMEOUT", "") or 600.0)

# Maps a configured backend provider env var to the cognee package "extra" that
# provides its driver. A bare `cognee==<version>` install has none of these, so
# the server crashes on first use of any non-default backend (#232): Postgres
# needs psycopg2/asyncpg (via `postgres-binary`), Neo4j needs its driver, a
# local Ollama LLM needs the `ollama` extra, and `fastembed` embeddings need
# their own extra. The env var names are the ones cognee's own config classes
# read (verified against 1.5.3's infrastructure/*/config.py field names, which
# pydantic-settings matches case-insensitively; GRAPH_DATABASE_PROVIDER is even
# explicit there). Values already sit in os.environ by the time the install
# runs: ensure_cognee_installed() calls apply_cognee_env() first, and the
# ~/.cognee/.env file is injected at process start (_env_file.py), so both
# exported and file-configured providers are seen. `postgres`/`pgvector` share
# one driver set, so VECTOR_DB_PROVIDER=pgvector maps to the same extra as
# DB_PROVIDER=postgres rather than a separate one.
_PROVIDER_EXTRAS = (
    ("DB_PROVIDER", {"postgres": "postgres-binary", "postgresql": "postgres-binary"}),
    ("VECTOR_DB_PROVIDER", {"pgvector": "postgres-binary"}),
    ("GRAPH_DATABASE_PROVIDER", {"neo4j": "neo4j"}),
    ("EMBEDDING_PROVIDER", {"fastembed": "fastembed"}),
    ("LLM_PROVIDER", {"ollama": "ollama"}),
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


# Install single-flight. Distinct from the server boot lock (which is short): a
# cold cognee install can take minutes, so concurrent sessions — across BOTH
# plugins, since the lock path is shared — must NOT install into the venv at once.
_VENV_INSTALL_LOCK = _GLOBAL_STATE_DIR / "venv-install.lock"
_VENV_INSTALL_LOCK_STALE_SECONDS = _INSTALL_TIMEOUT_SECONDS + 60.0
_VENV_INSTALL_WAIT_SECONDS = _INSTALL_TIMEOUT_SECONDS + 60.0
_VENV_INSTALL_POLL_SECONDS = 0.5


def _find_uv() -> str:
    """Locate uv: prefer our self-managed copy, then anything on PATH."""
    if _UV_BIN.exists():
        return str(_UV_BIN)
    found = shutil.which("uv")
    return found or ""


def _install_uv() -> str:
    """Install the standalone uv binary into ~/.cognee-plugin/uv (no PATH edits)."""
    try:
        _UV_DIR.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        # UV_UNMANAGED_INSTALL drops the binary in the given dir without editing
        # shell profiles or managing updates — exactly what we want.
        env["UV_UNMANAGED_INSTALL"] = str(_UV_DIR)
        subprocess.run(
            ["sh", "-c", f"curl -LsSf {_UV_INSTALL_URL} | sh"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if _UV_BIN.exists():
            return str(_UV_BIN)
    except Exception as exc:
        hook_log("uv_install_failed", {"error": str(exc)[:300]})
    return ""


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
# drivers are installed (verified against cognee 1.5.3's optional-dependencies).
# Probing the venv is deliberately preferred over recording installed extras in
# venv-ready.json: that marker is shared with plugins that don't know about
# extras (codex/openclaw), whose rewrites would wipe the record and force a
# reinstall on every cold boot after theirs — the exact churn skip-at-pin
# exists to avoid. `asyncpg` is common to the `postgres` and `postgres-binary`
# extras, so either variant satisfies the postgres-binary requirement.
_EXTRA_SENTINEL_DISTS = {
    "postgres-binary": "asyncpg",
    "neo4j": "neo4j",
    "fastembed": "fastembed",
    "ollama": "transformers",
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
        tmp = _VENV_READY_MARKER.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, _VENV_READY_MARKER)
    except Exception as exc:
        hook_log("venv_ready_write_failed", {"error": str(exc)[:200]})


def ensure_cognee_installed(timeout: float = _INSTALL_TIMEOUT_SECONDS) -> bool:
    """Ensure the shared plugin venv exists and holds the pinned cognee version.

    Called from the server-boot critical section (so it is already
    single-flighted) and only at boot points where the server is POSITIVELY
    absent (see server_presence) — a merely unresponsive server may be busy and
    still holding the graph store's file lock, and upgrading the venv under it
    corrupts the databases. Always installs the exact pinned version
    (_PINNED_COGNEE_VERSION) so the server's FastAPI lifespan migrations run on
    a known-good release, plus the extras the configured backend providers
    need (_cognee_install_spec, #232).

    Fails soft: if the install can't run (e.g. offline) but a usable cognee is
    already present, returns True with whatever version is there. Returns False
    only when no importable cognee venv exists afterwards.
    """
    apply_cognee_env()
    for directory in (_COGNEE_SYSTEM_DIR, _COGNEE_DATA_DIR, _COGNEE_CACHE_DIR):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            hook_log(
                "cognee_data_dir_mkdir_failed", {"dir": str(directory), "error": str(exc)[:200]}
            )

    owner = f"install:{os.getpid()}"
    acquired = False
    deadline = time.monotonic() + _VENV_INSTALL_WAIT_SECONDS
    try:
        _VENV_INSTALL_LOCK.parent.mkdir(parents=True, exist_ok=True)
        while True:
            now = time.time()
            if _VENV_INSTALL_LOCK.exists():
                stale = False
                try:
                    raw = json.loads(_VENV_INSTALL_LOCK.read_text(encoding="utf-8"))
                    pid = int(raw.get("pid", 0) or 0)
                    created_at = float(raw.get("created_at", 0) or 0)
                    stale = (not _pid_alive(pid)) or (
                        now - created_at > _VENV_INSTALL_LOCK_STALE_SECONDS
                    )
                except Exception:
                    stale = True
                if stale:
                    try:
                        _VENV_INSTALL_LOCK.unlink()
                    except Exception as exc:
                        hook_log("venv_install_lock_unlink_failed", {"error": str(exc)[:200]})

            try:
                fd = os.open(str(_VENV_INSTALL_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"owner": owner, "pid": os.getpid(), "created_at": now}, fh)
                acquired = True
                break
            except FileExistsError:
                # Another process owns the install. Don't install concurrently —
                # wait for it to produce a usable venv, then reuse it. The venv
                # must satisfy our extras too: the other installer may be an
                # extras-unaware plugin doing a bare install, in which case we
                # keep waiting for the lock and add the drivers ourselves.
                if _venv_cognee_version() == _PINNED_COGNEE_VERSION and not _venv_missing_extras(
                    _detect_required_extras()
                ):
                    return True
                if time.monotonic() >= deadline:
                    return bool(_venv_cognee_version())
                time.sleep(_VENV_INSTALL_POLL_SECONDS)

        # Already at the pin AND holding the configured providers' drivers:
        # there is nothing to install, so do not touch the venv. The install
        # used to run unconditionally at every cold boot; on a machine where
        # two plugins with different pins share this venv that made each boot
        # flip the version back and forth — and a `pip install` is a mutation
        # window the shared runtime is better off without. The version alone
        # is not enough, though: a provider configured after the venv was
        # built leaves the pin satisfied with its drivers missing (#232), so
        # extras are probed too and a miss falls through to the install.
        # The marker is (re)written so every plugin reading it sees the truth.
        current = _venv_cognee_version()
        required_extras = _detect_required_extras()
        missing_extras = _venv_missing_extras(required_extras)
        if current == _PINNED_COGNEE_VERSION:
            if not missing_extras:
                _write_venv_ready(current)
                hook_log(
                    "cognee_install_skipped_at_pin",
                    {"version": current, "extras": required_extras},
                )
                return True
            hook_log("cognee_extras_missing_at_pin", {"missing": missing_extras})

        uv = _find_uv() or _install_uv()
        venv_present = _VENV_PYTHON.exists()

        if uv:
            env = os.environ.copy()
            env.setdefault("UV_PYTHON_INSTALL_DIR", str(_UV_PYTHON_DIR))
            try:
                if not venv_present:
                    subprocess.run(
                        [uv, "venv", str(_VENV_DIR), "--python", _PINNED_PYTHON],
                        env=env,
                        cwd=str(_GLOBAL_STATE_DIR),
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                    )
                subprocess.run(
                    [
                        uv,
                        "pip",
                        "install",
                        "--upgrade",
                        "--python",
                        str(_VENV_PYTHON),
                        _cognee_install_spec(),
                    ],
                    env=env,
                    cwd=str(_GLOBAL_STATE_DIR),
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except Exception as exc:
                hook_log("cognee_install_failed", {"via": "uv", "error": str(exc)[:300]})
        elif not venv_present:
            # Last-resort fallback: stdlib venv + pip. Slower, and relies on the
            # system python3 being a cognee-compatible version (3.10-3.14).
            try:
                subprocess.run(
                    [sys.executable, "-m", "venv", str(_VENV_DIR)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                subprocess.run(
                    [
                        str(_VENV_PYTHON),
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        _cognee_install_spec(),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except Exception as exc:
                hook_log("cognee_install_failed", {"via": "venv_pip", "error": str(exc)[:300]})

        version = _venv_cognee_version()
        if not version:
            hook_log("cognee_venv_unusable", {"venv_python": str(_VENV_PYTHON)})
            return False
        _write_venv_ready(version)
        hook_log("cognee_install_ready", {"version": version, "extras": required_extras})
        return True
    finally:
        if acquired:
            try:
                _VENV_INSTALL_LOCK.unlink()
            except Exception as exc:
                hook_log("venv_install_lock_release_failed", {"error": str(exc)[:200]})


def _parse_host_port(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(url if "://" in url else f"http://{url}")
    return (parsed.hostname or "localhost"), (parsed.port or 8011)


def _is_local_url(url: str) -> bool:
    """True if the URL points at this machine (so we may boot a server on it)."""
    host, _ = _parse_host_port(url)
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")


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
        with urllib.request.urlopen(url, timeout=timeout, context=_https_context()) as response:
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

    # The server is positively absent and we're at a boot point: ensure the
    # shared venv holds the latest cognee BEFORE booting, so the server's
    # lifespan migrations run on the upgraded code. Single-flighted on its own
    # (long) lock, separate from the short boot lock below, since a cold
    # install can take minutes.
    if not ensure_cognee_installed():
        raise RuntimeError("cognee runtime unavailable (install/upgrade failed)")

    # A cold install can take minutes — long enough for the world to change.
    # Re-verify the premise before proceeding to the boot lock.
    if _require_absent("post_install"):
        _ready()
        return

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

        server_env = os.environ.copy()
        # Data-dir pins + CACHING are already in os.environ via apply_cognee_env(),
        # so the copy carries them to the server process.
        # We are spawning the server, so run it in agent mode: it tears itself
        # down once all registered agents disconnect.
        server_env["COGNEE_AGENT_MODE"] = "true"
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
        write_server_pidfile(port, server_proc.pid, version=_PINNED_COGNEE_VERSION)

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


async def _login_default_user_for_owner_api_key(service_url: str, config: dict) -> str:
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
        raise RuntimeError(
            "default-user login failed "
            f"({status}: {body[:200]}). "
            "Set COGNEE_USER_EMAIL/COGNEE_USER_PASSWORD correctly."
        )
    login_data = json.loads(body) if body else {}
    jwt = str(login_data.get("access_token", "") or "")
    if not jwt:
        raise RuntimeError("default-user login returned no access token")

    status, body = _cloud_http_request(
        f"{base}/api/v1/auth/api-keys",
        method="GET",
        cookies={"auth_token": jwt},
        timeout=30.0,
    )
    if status == 200:
        keys = json.loads(body) if body else []
        if isinstance(keys, list) and keys:
            key = str(keys[0].get("key", "") or "")
            if key:
                return key

    status, body = _cloud_http_request(
        f"{base}/api/v1/auth/api-keys",
        method="POST",
        json_body={"name": "codex-owner-bootstrap"},
        cookies={"auth_token": jwt},
        timeout=30.0,
    )
    if status != 200:
        raise RuntimeError(f"default-user API key creation failed ({status}: {body[:200]})")
    payload = json.loads(body) if body else {}
    key = str(payload.get("key", "") or "")
    if not key:
        raise RuntimeError("default-user API key creation returned empty key")
    return key


def _resolve_agent_name(config: dict, cwd: str) -> str:
    def _normalize(name: str) -> str:
        raw = str(name or "").strip()
        if raw.endswith("@cognee.agent"):
            raw = raw[: -len("@cognee.agent")]
        suffix = "_codex"
        if raw.endswith(suffix):
            return raw
        return f"{raw}{suffix}"

    configured = str(config.get("agent_name", "") or "").strip()
    if configured:
        return _normalize(configured)
    return _normalize(f"codex-{Path(cwd).name}")


async def _resolve_single_principal_key(service_url: str, config: dict) -> str:
    """Resolve the PRINCIPAL key for this deployment.

    Order: env ``COGNEE_API_KEY`` -> single cached key -> mint once from the
    default user (and cache it).

    The provisioned plugin-agent key is explicitly NOT a principal: earlier
    steps in this same process may have stamped it into the env / config (see
    ``_api_key_with_source``), and provisioning authenticated as the agent
    would nest a new agent under the agent. Skip it wherever it leaked in.
    """
    from _plugin_common import (
        load_cached_agent_key,
        load_cached_api_key,
        save_cached_api_key,
    )

    agent_key = load_cached_agent_key(service_url)
    api_key = str(config.get("api_key", "") or os.environ.get("COGNEE_API_KEY", "")).strip()
    if agent_key and api_key == agent_key:
        api_key = ""
    if not api_key:
        api_key = os.environ.get("COGNEE_PRINCIPAL_API_KEY", "") or load_cached_api_key(service_url)
    if not api_key:
        api_key = await _login_default_user_for_owner_api_key(service_url, config)
        if api_key:
            save_cached_api_key(service_url, api_key)
    return api_key


async def _ensure_plugin_identity(service_url: str, config: dict, principal_key: str) -> str:
    """Explicit opt-in; never rotate or silently fall back to the owner."""
    from _plugin_common import (
        _AGENT_KEY_CACHE,
        _load_json_file,
        _principal_fingerprint,
        load_cached_agent_key,
        plugin_identity_lock,
        plugin_identity_mode,
        provision_plugin_agent_via_http,
        save_cached_agent_key,
    )

    mode = plugin_identity_mode(config)
    if mode == "disabled":
        return ""
    with plugin_identity_lock():
        key = load_cached_agent_key(service_url)
        record = _load_json_file(_AGENT_KEY_CACHE)
        if key:
            if record.get("blocked"):
                raise RuntimeError(
                    "Plugin identity is disconnected; explicit reconnect is required"
                )
            if record.get("principal_fingerprint") != _principal_fingerprint(principal_key):
                raise RuntimeError(
                    "Cached plugin identity does not match this principal; reconnect explicitly"
                )
            return key
        if mode != "enabled":
            return ""
        status, body = provision_plugin_agent_via_http(
            principal_key=principal_key, service_url=service_url
        )
        if status != "provisioned":
            raise RuntimeError(
                f"Plugin provisioning {status}; owner fallback is disabled. "
                "Safe create-only SDK support is required."
            )
        key = str(body.get("api_key") or "").strip()
        save_cached_agent_key(
            service_url, key, str(body.get("agent_id") or ""), principal_key=principal_key
        )
        return key


async def _ensure_agent_credentials_and_register(
    config: dict, cwd: str, session_id: str, agent_session_name: str, session_key: str
) -> tuple[str, str, str, bool]:
    service_url = _normalize_service_url(str(config.get("base_url", "") or ""))
    if not service_url:
        return "", "", "", False

    from _plugin_common import block_cached_agent_key, register_agent_via_http

    principal_key = await _resolve_single_principal_key(service_url, config)
    if not principal_key:
        return "", "", "", False

    os.environ["COGNEE_PRINCIPAL_API_KEY"] = principal_key
    agent_key = await _ensure_plugin_identity(service_url, config, principal_key)
    api_key = agent_key or principal_key

    os.environ["COGNEE_API_KEY"] = api_key
    config["api_key"] = api_key

    # The effective identity's user id (best-effort) — used for dataset
    # readiness + watchers. Under a plugin identity this is the agent
    # sub-user, which is exactly the user the data plane operates as.
    user_id = await _user_id_via_api(service_url, api_key)

    # Registration is a lifecycle counter + connection registry under the
    # effective identity. The connection handle IS the Cognee session id.
    registered, registration = register_agent_via_http(
        agent_session_name=agent_session_name,
        session_id=session_id,
        dataset_names=[str(config.get("dataset", "") or "").strip()],
    )
    if not registered and registration.get("auth_failed") and agent_key:
        block_cached_agent_key(agent_key)
        raise RuntimeError(
            "Plugin identity rejected; automatic re-provision and owner fallback are disabled"
        )
    if not registered:
        raise RuntimeError(f"Failed to register session '{session_id}' on {service_url}.")

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


def _find_codex_parent_pid() -> int:
    """Find the nearest live Codex ancestor, skipping hook shells."""
    fallback = os.getppid()
    if sys.platform == "win32":
        return find_host_ancestor_windows(fallback, "codex")
    try:
        raw = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        hook_log("find_codex_parent_failed", {"error": str(exc)[:200]})
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

    # Match "codex" as an executable basename anywhere in the command line,
    # tolerant of spaces in the executable path (a naive split()[0] mis-tokenizes
    # paths like "/…/Application Support/…/codex").
    host_re = re.compile(r"(?:^|/)codex(?:-[\w.]+)?(?:\s|$)")
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
    """Launch a detached watcher that syncs only after Codex exits."""

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

    parent_pid = _find_codex_parent_pid()
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
        "api_key": api_key,
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
    # mistaken for live configuration. ~/.cognee/.env is the one setup file.
    legacy_config = _GLOBAL_STATE_DIR / "config.json"
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
) -> None:
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
    except Exception as exc:
        hook_log("bootstrap_spawn_failed", {"error": str(exc)[:300]})


def _status_from_error(message: str) -> int:
    """Extract an HTTP status embedded as ``(<status>: ...)`` in an error string.

    ``ensure_cognee_ready`` and ``ensure_dataset_ready_via_api`` both format their
    RuntimeError as ``... (<status>: <body>)``; return 0 when none is present
    (e.g. a connection error carries no HTTP status).
    """
    import re

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
) -> tuple[str, str, bool]:
    """Slow session bootstrap shared by the inline (legacy) path and the
    detached worker: server boot wait, cognee init, agent registration, dataset
    creation, and the server-ready marker.

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
    # (via ensure_cognee_installed) has now built the shared venv, so flip into
    # it before any cognee/aiohttp import below resolves against the host. No-op
    # when the venv is absent (connect/managed mode) or already inside it.
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
            if _conn_state is None:
                try:
                    health = probe_health(
                        _normalize_service_url(str(config.get("base_url", "") or "")), timeout=1.5
                    )
                except Exception:
                    health = "unknown"
                if health == "ready":
                    _conn_state, _conn_detail = "auth_failed", message
                elif health == "down":
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
    else:
        # Local SDK fallback path.
        try:
            if not user_id:
                user_id, fallback_key = await ensure_identity(config)
                if fallback_key and not agent_api_key:
                    agent_api_key = fallback_key
        except Exception as e:
            print(f"cognee-plugin: identity warning ({e})", file=sys.stderr)

    try:
        # Cloud: the API key IS the identity (the server derives the principal
        # from X-Api-Key), so dataset creation must NOT be gated on user_id —
        # servers without /users/me (e.g. cloud tenants) leave user_id empty
        # while auth works fine. Only the SDK branch below needs a User object.
        if is_cloud_mode(config):
            await ensure_dataset_ready_via_api(
                config.get("base_url", ""),
                agent_api_key or config.get("api_key", ""),
                dataset,
            )
        elif user_id:
            from uuid import UUID

            from cognee.modules.users.methods import get_user

            user = await get_user(UUID(user_id))
            await ensure_dataset_ready(dataset, user)
    except Exception as e:
        print(f"cognee-plugin: dataset warning ({e})", file=sys.stderr)
        if is_cloud_mode(config):
            status = _status_from_error(e)
            # An authed 401/403 here is a definitive auth failure — let it win.
            if status in (401, 403):
                _conn_state, _conn_detail = "auth_failed", str(e)[:200]
            elif status >= 500 and _conn_state is None:
                _conn_state, _conn_detail = "server_error", str(e)[:200]
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

    # Code graph: index the repository this session opened in, or refresh it if
    # the tree moved while the agent was away. Deliberately last and inside the
    # detached worker — it must never delay the first prompt — and best-effort:
    # a failure here costs code facts, not the session.
    try:
        from _code_graph import autoindex_on_session_start

        code_outcome = autoindex_on_session_start(
            cwd,
            service_url,
            os.environ.get("COGNEE_API_KEY", ""),
            is_local_server=service_url_is_local(service_url),
        )
        if code_outcome:
            hook_log("code_autoindex", code_outcome)
    except Exception as exc:
        hook_log("code_autoindex_error", {"error": str(exc)[:200]})

    return user_id, agent_api_key, True


async def _run_bootstrap(bootstrap: dict) -> None:
    """Detached worker body.

    Two distinct concerns, deliberately decoupled:
      1. Boot the local server EXACTLY ONCE — single-flighted on _BOOTSTRAP_LOCK
         so concurrent agents don't each spawn uvicorn. Workers that don't win
         the lock just wait for /health instead of returning.
      2. Register THIS agent/session — runs for EVERY agent, regardless of who
         booted, because registration is per-agent (and concurrency-safe via
         the agent-keys / agent-lifecycle locks inside _run_heavy).
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
            return

    # 2. Register this agent/session. Runs for every agent — NOT gated by the
    #    boot single-flight. _run_heavy's _ensure_local_server_running is a
    #    no-op now that the server is healthy.
    try:
        await _run_heavy(
            config,
            cwd,
            session_id,
            agent_session_name,
            session_key,
            dataset,
            managed_endpoint=False,
            boot_timeout=_SERVER_BOOT_DEADLINE_SECONDS,
        )
        hook_log("bootstrap_complete", {"session_id": session_id})
    except Exception as exc:
        hook_log("bootstrap_failed", {"error": str(exc)[:300]})


async def _start(payload: dict | None = None) -> dict:
    config = load_config()
    payload = payload or {}
    cwd = str(payload.get("cwd") or os.environ.get("CODEX_CWD") or os.getcwd())
    # The service URL is the sole router (api_key is optional auth, with a
    # default-user fallback in registration). COGNEE_AGENT_MODE is NOT decided
    # here: it's set only when we actually boot a server (in
    # _ensure_local_server_running), so connecting to an already-running server
    # never claims ownership of its teardown.
    configured_url = _with_scheme(str(config.get("base_url", "") or "").strip())
    api_key = str(config.get("api_key", "") or "").strip()
    # Forced cloud (backend switch) with no URL configured: there is nothing to
    # boot and nothing to fall back to — keep the target empty so no local
    # server is spawned, let the connection attempt fail, and let the status
    # line report the missing URL. Everything else keeps the localhost default.
    forced_cloud_unconfigured = not configured_url and config.get("_forced_backend") == "cloud"
    target_url = configured_url or ("" if forced_cloud_unconfigured else _LOCAL_SERVICE_URL)
    config["base_url"] = target_url
    os.environ["COGNEE_BASE_URL"] = target_url
    if api_key:
        os.environ["COGNEE_API_KEY"] = api_key

    # NOTE: the local server's LLM_API_KEY health is deliberately NOT judged here.
    # A hook-env read (config's llm_api_key / OPENAI_API_KEY) is blind to a key that
    # lives in cognee's own config or a .env the server loads, so a session launched
    # from a shell without the export wrote "not_set" into a marker that EVERY
    # session's status line reads — a false ✕ (incorrect_llm_api_key) for sessions whose key is
    # fine. The idle watcher is the sole authority instead: it resolves the key the
    # same way the server does (cognee's get_llm_config) and validates it against
    # the provider, so the verdict matches reality. It runs at session start, so the
    # signal still appears within seconds of launch.

    # The host (Codex) session id is a local correlation key only: it keeps every
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
        hook_log("missing_payload_session_id", {"cwd": cwd})
        print(
            "cognee-plugin: missing payload session_id; refusing to register",
            file=sys.stderr,
        )
        return {
            "hookSpecificOutput": {"hookEventName": "SessionStart"},
        }
    os.environ["COGNEE_SESSION_KEY"] = session_key

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
        host_pid=_find_codex_parent_pid(),
    )
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
    presence_verdict, presence_evidence = "", {}
    if target_url and _is_local_url(target_url):
        presence_verdict, presence_evidence = server_presence(target_url, confirm_absent=False)
        server_live = presence_verdict == PRESENCE_READY
    else:
        server_live = bool(target_url) and _health_ok(_health_url(target_url))
    will_boot = (not server_live) and bool(target_url) and _is_local_url(target_url)
    hook_log(
        "endpoint_mode_selected",
        {
            "base_url": target_url,
            "server_live": server_live,
            "will_boot": will_boot,
            **(
                {"presence": presence_verdict, "evidence": presence_evidence}
                if presence_verdict
                else {}
            ),
        },
    )
    if will_boot and _LAZY_BOOTSTRAP:
        _spawn_bootstrap(config, cwd, session_id, agent_session_name, session_key, dataset)
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
            if _LAZY_BOOTSTRAP and target_url and _is_local_url(target_url):
                # Inline attempt failed; retry the heavy path out of band.
                _spawn_bootstrap(config, cwd, session_id, agent_session_name, session_key, dataset)
            else:
                return {}

    # Remove legacy resolved cache files. Runtime state now comes from HTTP endpoints.
    _purge_legacy_resolved_files()
    # Per-session files nothing else deletes (launch records, status markers,
    # bridge caches, pending buffers, dead improve locks) and oversized logs.
    from _plugin_common import sweep_stale_state

    sweep_stale_state()

    # Reset the idle clock for this Codex process before the watcher
    # starts, otherwise a stale timestamp from a prior session can cause
    # an immediate improve on startup.
    touch_activity()

    # Launch the idle watcher (syncs session memory to graph after the agent
    # goes idle). Runs in both local-server and cloud modes — the watcher picks
    # the HTTP or local sync path itself. COGNEE_IDLE_DISABLED opts out.
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

    mode = "cloud" if config.get("base_url") else "local"
    print(
        f"cognee-plugin: session ready (mode={mode}, "
        f"session={session_id}, dataset={dataset}, user={user_id[:8]}...)",
        file=sys.stderr,
    )

    status_line = render_status_for_host(session_key)
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": status_line + _update_nudge_suffix(),
        },
    }


def _update_nudge_suffix() -> str:
    """One-time 'update available' line appended to SessionStart context.

    Reads the marker written by the idle watcher's background check (no network),
    shown once per newly-detected version (tracked via ``notified_version``). The
    per-turn in-context status (render_status_for_host) carries the short ambient
    segment; this is the actionable one-shot nudge with the update command.
    """
    try:
        from _plugin_common import mark_update_notified, read_update_status

        status = read_update_status()
    except Exception:
        return ""
    if not status:
        return ""
    installed = str(status.get("installed_version") or "")
    latest = str(status.get("latest_version") or "")
    if not (installed and latest) or status.get("notified_version") == latest:
        return ""
    try:
        mark_update_notified(latest)
    except Exception:
        pass
    return (
        f"\n\nCognee update available {installed} → {latest} — run "
        "`codex plugin marketplace upgrade cognee` to update."
    )


def main():
    # First run: leave a commented ~/.cognee/.env template so one-time config
    # has a documented file to land in. Values (if any) were already loaded
    # into os.environ when _plugin_common was imported.
    from _env_file import ensure_env_file_template, env_file_path

    if ensure_env_file_template():
        hook_log("env_file_template_created", {"path": str(env_file_path())})

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

    # Keep SessionStart output as a valid empty hook result. Recall context is
    # injected on UserPromptSubmit, matching the original Codex hook contract.
    output = {}
    try:
        with quiet_hook_output("session-start"):
            output = asyncio.run(_start(payload)) or {}
    except Exception as exc:
        hook_log("session_start_exception", {"error": str(exc)[:200]})
    print(json.dumps(output))


if __name__ == "__main__":
    main()
