"""Ensure a local cognee HTTP server is running, so Hermes can be a thin client.

Why a server instead of the in-process SDK: cognee's local stores (SQLite
relational, Kuzu/Ladybug graph, LanceDB vector) are **single-writer**. Driving
them in-process from Hermes's background threads — or from two Hermes processes
sharing one data dir — risks "database is locked"/corruption. cognee uses a
``DatasetQueue`` + subprocess DB workers precisely because the HTTP server is the
intended *single owner* that serializes access. So local mode points the SDK at a
local server (``cognee.serve(url)``) and lets the server own the databases.

This mirrors the proven Claude/Codex bootstrap: health-check, spawn uvicorn if
needed, poll ``/health``. No explicit lock is needed — only one process can bind
the port, so concurrent spawns simply lose the bind and then observe health.
"""

import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .config import DEFAULT_SERVER_BOOT_TIMEOUT, SHARED_COGNEE_HOME, SHARED_PLUGIN_STATE_DIR

logger = logging.getLogger(__name__)


def health_ok(url, timeout=2.0):
    """True when GET {url}/health returns a 2xx."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(status) < 300
    except Exception:
        return False


def port_bound(port, timeout=1.0):
    """True when something accepts TCP connections on the local port.

    Weaker than :func:`health_ok` on purpose: uvicorn binds the port well before
    the app is ready, so this distinguishes "a server is coming up" from "nothing
    is there at all".
    """
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _spawn(port, data_root, system_root, log_path):
    env = dict(os.environ)
    env["COGNEE_AGENT_MODE"] = "true"  # server tears itself down once idle / no clients
    env["HTTP_API_PORT"] = str(port)
    # The server must behave identically no matter which cognee plugin booted it,
    # so this mirrors the env the claude-code/codex/openclaw bootstraps set
    # (claude-code's apply_cognee_env). setdefault: an explicit user value wins.
    #
    # CACHING gates the session cache: without it cognee's session manager reports
    # ``is_available = False`` and a session write is *silently dropped* while the
    # API still answers ``status: "session_stored"`` — so every turn vanished and
    # improve() had nothing to promote into the graph (live-diagnosed).
    # LLM_INSTRUCTOR_MODE=json_schema_mode uses grammar-constrained decoding for
    # structured output — small local models fail schema-in-prompt mode so often
    # that every improve() timed out (diagnosed in the claude-code plugin).
    for key, value in (
        ("CACHING", "true"),
        ("AUTO_FEEDBACK", "true"),
        ("CACHE_ROOT_DIRECTORY", str(SHARED_COGNEE_HOME / "cache")),
        ("LLM_INSTRUCTOR_MODE", "json_schema_mode"),
        ("COGNEE_IMPROVE_SUBMIT_TIMEOUT", "420"),
    ):
        env.setdefault(key, value)
    if data_root:
        env["DATA_ROOT_DIRECTORY"] = data_root
    if system_root:
        env["SYSTEM_ROOT_DIRECTORY"] = system_root
    try:
        log = open(log_path, "ab", buffering=0)  # noqa: SIM115 — handed to the child
    except Exception:
        log = subprocess.DEVNULL
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "cognee.api.client:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,  # detach: outlive the spawning call
        )
    finally:
        # The child inherited its own dup of the fd; close the parent's copy so we
        # don't leak a descriptor on every initialize().
        if log is not subprocess.DEVNULL:
            log.close()


def ensure_local_server(
    port,
    *,
    data_root="",
    system_root="",
    log_path=None,
    boot_timeout=float(DEFAULT_SERVER_BOOT_TIMEOUT),
):
    """Return the URL of a healthy local cognee server, starting one if needed.

    The deadline is generous (matching the other plugins' 600s) because a *first*
    boot runs migrations and can take minutes — but it is only ever waited out
    for a server that is actually coming up. A dead spawn with nothing listening
    on the port raises immediately: waiting cannot fix a missing dependency or a
    crash, and a 10-minute hang would stall every Hermes session start.

    Raises RuntimeError when the spawn is dead and the port unowned, or when the
    server does not become healthy within boot_timeout.
    """
    url = "http://127.0.0.1:%d" % int(port)
    if health_ok(url):
        return url
    if log_path is None:
        # Same state root as the other cognee plugins (they keep per-host logs in
        # ~/.cognee-plugin/<host>/), so diagnostics live in one predictable place.
        log_dir = SHARED_PLUGIN_STATE_DIR / "hermes"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # _spawn falls back to DEVNULL if the file cannot be opened
        log_path = str(log_dir / "server.log")
    proc = None
    spawn_error = None
    try:
        proc = _spawn(port, data_root, system_root, log_path)
    except Exception as exc:
        # A spawn failure may just be a port-bind race with another starter, in
        # which case health polling below will still succeed. But it may also be a
        # real problem (missing uvicorn, permission denied) — log it for diagnostics.
        spawn_error = exc
        logger.warning("cognee server spawn attempt failed (will still poll /health): %s", exc)
    deadline = time.monotonic() + float(boot_timeout)
    while time.monotonic() < deadline:
        if health_ok(url):
            return url
        # Our spawn being gone is not itself fatal — losing a port-bind race to a
        # concurrent starter looks exactly like this, and then the port has an
        # owner worth waiting for. Dead spawn AND a silent port is fatal.
        spawn_is_gone = spawn_error is not None or (proc is not None and proc.poll() is not None)
        if spawn_is_gone and not port_bound(port):
            cause = (
                "failed to launch (%s)" % spawn_error
                if spawn_error is not None
                else "exited with code %s" % proc.returncode
            )
            raise RuntimeError(
                "cognee local server %s and nothing is listening on port %s. "
                "See %s for the server output." % (cause, port, log_path)
            )
        time.sleep(1.0)
    raise RuntimeError(
        "cognee local server did not become healthy at %s within %ss. "
        "See %s for the server output." % (url, boot_timeout, log_path)
    )
