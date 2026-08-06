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
import subprocess
import sys
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def health_ok(url, timeout=2.0):
    """True when GET {url}/health returns a 2xx."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(status) < 300
    except Exception:
        return False


def _spawn(port, data_root, system_root, log_path):
    env = dict(os.environ)
    env["COGNEE_AGENT_MODE"] = "true"  # server tears itself down once idle / no clients
    env["HTTP_API_PORT"] = str(port)
    if data_root:
        env["DATA_ROOT_DIRECTORY"] = data_root
    if system_root:
        env["SYSTEM_ROOT_DIRECTORY"] = system_root
    try:
        log = open(log_path, "ab", buffering=0)  # noqa: SIM115 — handed to the child
    except Exception:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen(
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
    boot_timeout=30.0,
):
    """Return the URL of a healthy local cognee server, starting one if needed.

    Raises RuntimeError if the server does not become healthy within boot_timeout.
    """
    url = "http://127.0.0.1:%d" % int(port)
    if health_ok(url):
        return url
    if log_path is None:
        log_path = os.path.join(os.path.expanduser("~"), ".cognee-hermes-server.log")
    try:
        _spawn(port, data_root, system_root, log_path)
    except Exception as exc:
        # A spawn failure may just be a port-bind race with another starter, in
        # which case health polling below will still succeed. But it may also be a
        # real problem (missing uvicorn, permission denied) — log it for diagnostics.
        logger.warning("cognee server spawn attempt failed (will still poll /health): %s", exc)
    deadline = time.monotonic() + float(boot_timeout)
    while time.monotonic() < deadline:
        if health_ok(url):
            return url
        time.sleep(1.0)
    raise RuntimeError(
        "cognee local server did not become healthy at %s within %ss" % (url, boot_timeout)
    )
