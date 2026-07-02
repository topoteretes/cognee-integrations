"""Shared helpers for the opt-in integration smoke tests.

Standardizes on Hermes's server bootstrap
(``integrations/hermes-agent/cognee_integration_hermes/server_bootstrap.py``):
pick a free port, spawn ``uvicorn cognee.api.client:app``, poll ``/health``.
Kept intentionally small and stdlib-only so it can live *verbatim* next to each
scripts-only plugin (claude-code, codex) — those have no shared importable
package, so "reuse" here means an identical, copy-pasted module rather than a
cross-directory import (which the isolated per-integration CI runs can't resolve).

The smoke tests need the full cognee stack **and** LLM creds for the write path,
so they are OPT-IN: set ``COGNEE_RUN_INTEGRATION=1`` (and have cognee installed)
to run them. Skipped by default — that keeps CI green without creds.
"""

import importlib.util
import os
import socket
import subprocess
import sys
import time
import urllib.request

# The single documented switch, matching Hermes so there's one env var across
# the monorepo. Both conditions must hold or the smoke is skipped.
RUN = os.environ.get("COGNEE_RUN_INTEGRATION") == "1"
HAS_COGNEE = importlib.util.find_spec("cognee") is not None
REASON = "set COGNEE_RUN_INTEGRATION=1 and install cognee to run integration smoke tests"


def free_port() -> int:
    """An ephemeral loopback port. Binding :0 lets the OS pick a free one."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def health_ok(url: str, timeout: float = 2.0) -> bool:
    """True when GET {url}/health returns a 2xx."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(status) < 300
    except Exception:
        return False


def spawn_server(port: int, data_root: str, system_root: str, log_path: str) -> subprocess.Popen:
    """Spawn a local cognee HTTP server on ``port`` with isolated data dirs.

    ``DATA_ROOT_DIRECTORY``/``SYSTEM_ROOT_DIRECTORY`` point at a throwaway tmp dir so
    the smoke never touches the developer's real cognee data. We deliberately do NOT
    set ``COGNEE_AGENT_MODE`` (which makes the server self-terminate when idle): the
    test owns this process's lifecycle and tears it down explicitly.
    """
    env = dict(os.environ)
    env["HTTP_API_PORT"] = str(port)
    env["DATA_ROOT_DIRECTORY"] = data_root
    env["SYSTEM_ROOT_DIRECTORY"] = system_root
    log = open(log_path, "ab", buffering=0)  # noqa: SIM115 — handed to the child
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
            start_new_session=True,
        )
    finally:
        # The child dup'd its own fd; close the parent's copy so we don't leak one.
        log.close()


def wait_healthy(url: str, deadline_s: float = 90.0) -> bool:
    """Poll /health until healthy or the deadline (cold cognee boots can be slow)."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if health_ok(url):
            return True
        time.sleep(1.0)
    return False
