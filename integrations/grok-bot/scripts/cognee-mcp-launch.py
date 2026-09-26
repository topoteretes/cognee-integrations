#!/usr/bin/env python3
"""Launch ``cognee-mcp`` (stdio) against the plugin's configured Cognee server.

Grok Build attaches a trusted plugin's ``.mcp.json`` servers to every session,
and this is what ``.mcp.json`` runs. It gives the model ``remember`` / ``recall``
/ ``forget`` tools that share the same server, dataset and credentials the
plugin's hooks use, so memory reaches the model even where the host does not
deliver hook output (Grok discards ``UserPromptSubmit`` ``additionalContext``,
and this Grok build does not dispatch plugin hooks in headless sessions at all).

Resolution mirrors the shell wrappers: ``~/.cognee/.env`` is loaded first (shell
exports still win), then ``COGNEE_BASE_URL`` / ``COGNEE_API_KEY`` fall back to
the local server URL and the auto-minted key cached in ``api_key.json``.

Local mode: when the resolved server is local and not answering, the plugin's
own SessionStart bootstrap is started (detached, the same code the hook runs)
and the launcher waits a bounded time for ``/health`` before handing off, so
the first tool call finds a server. The hand-off is always ``cognee-mcp`` in
API/proxy mode (``--api-url``), never an embedded cognee: two processes must
not open the plugin server's databases.

Stdout is the MCP JSON-RPC channel; every diagnostic goes to stderr, which Grok
writes to ``~/.grok/logs/mcp/<server>.stderr.log``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# The venv re-exec is for hooks that import cognee; this launcher only resolves
# configuration and execs another program, so the host python is enough.
os.environ.setdefault("COGNEE_PLUGIN_IN_VENV", "1")

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _env_file import load_env_file  # noqa: E402
from _plugin_common import (  # noqa: E402
    _normalize_service_url,
    hook_log,
    probe_health,
    resolved_http_endpoint_auth,
)

# Bounded so the whole handshake (this wait + cognee-mcp's own start-up) stays
# inside Grok's 30-second MCP init timeout. A server still warming after this
# is handed off anyway: cognee-mcp proxies lazily, so tools work once it is up.
BOOT_WAIT_SECONDS = float(os.environ.get("COGNEE_MCP_BOOT_WAIT_SECONDS", "15") or 15)
HEALTH_POLL_SECONDS = 1.0


def _log(msg: str) -> None:
    sys.stderr.write(f"[cognee-mcp-launch] {msg}\n")
    sys.stderr.flush()


def _is_local(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def _start_local_bootstrap(cwd: str) -> None:
    """Run the plugin's SessionStart bootstrap detached, as a hook would.

    Uses a synthetic session id so the boot is attributed to this MCP launch;
    the bootstrap's own single-flight locks make a concurrent hook-driven boot
    (a TUI session that does dispatch hooks) a no-op.
    """
    payload = json.dumps(
        {
            "hookEventName": "session_start",
            "sessionId": f"grok-mcp-{os.getpid()}",
            "cwd": cwd,
            "workspaceRoot": cwd,
        }
    )
    script = SCRIPTS_DIR / "session-start.py"
    env = dict(os.environ)
    env.pop("COGNEE_PLUGIN_IN_VENV", None)  # the bootstrap does need the venv
    env.setdefault("GROK_PLUGIN_ROOT", str(SCRIPTS_DIR.parent))
    try:
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(payload.encode("utf-8"))
        proc.stdin.close()
        _log(f"local server not answering; started plugin bootstrap (pid {proc.pid})")
    except Exception as exc:  # noqa: BLE001 - never block the MCP hand-off
        _log(f"could not start plugin bootstrap: {exc}")


def _wait_for_health(url: str) -> bool:
    deadline = time.monotonic() + BOOT_WAIT_SECONDS
    while time.monotonic() < deadline:
        if probe_health(url, timeout=1.0) == "ready":
            return True
        time.sleep(HEALTH_POLL_SECONDS)
    return False


# cognee-mcp is pinned to the same release the plugin's own server pin tracks;
# override with COGNEE_MCP_SPEC (any `uvx --from` requirement, e.g. a path).
COGNEE_MCP_SPEC = os.environ.get("COGNEE_MCP_SPEC", "cognee-mcp==0.5.5")
# Interpreter for the uvx environment. cognee's dependency tree needs 3.11+
# (``typing.NotRequired``), and uvx otherwise picks whatever python3 is first
# on PATH; 3.12 matches the plugin's own server venv, so uv already manages it.
COGNEE_MCP_PYTHON = os.environ.get("COGNEE_MCP_PYTHON", "3.12")


def _find_cognee_mcp() -> tuple[list[str], dict[str, str]]:
    """(command prefix, extra env) that runs cognee-mcp.

    Prefers ``uvx`` (no install step for the user; the first run downloads the
    package, later runs start from uv's cache). Falls back to a ``cognee-mcp``
    binary already on PATH.

    Workaround, cognee-mcp 0.5.5: its console script imports ``server`` as a
    top-level module (``from server import main``), which only resolves when
    the package's ``src/`` directory is on ``sys.path`` — true for a checkout
    run with ``uv --directory``, false for a wheel install, where it dies with
    ``ModuleNotFoundError: No module named 'server'``. Putting that ``src/``
    directory on PYTHONPATH restores it; the lookup costs one short python call.
    """
    uvx = shutil.which("uvx")
    if uvx:
        env: dict[str, str] = {}
        try:
            src_dir = subprocess.run(
                [uvx, "--python", COGNEE_MCP_PYTHON, "--from", COGNEE_MCP_SPEC, "python", "-c",
                 "import os, src; print(os.path.dirname(src.__file__))"],
                capture_output=True, text=True, timeout=120, check=False,
            ).stdout.strip().splitlines()[-1:]
            if src_dir and os.path.isdir(src_dir[0]):
                existing = os.environ.get("PYTHONPATH", "")
                env["PYTHONPATH"] = src_dir[0] + (os.pathsep + existing if existing else "")
        except Exception as exc:  # noqa: BLE001 - fall through; cognee-mcp may be fixed
            _log(f"could not locate cognee-mcp's src dir for the import workaround: {exc}")
        return [uvx, "--python", COGNEE_MCP_PYTHON, "--from", COGNEE_MCP_SPEC, "cognee-mcp"], env
    binary = shutil.which("cognee-mcp")
    if binary:
        return [binary], {}
    return [], {}


def main() -> int:
    load_env_file()
    service_url, api_key = resolved_http_endpoint_auth()
    service_url = _normalize_service_url(service_url)
    cwd = os.environ.get("GROK_WORKSPACE_ROOT") or os.getcwd()

    if not service_url:
        _log("no Cognee server URL resolved; set COGNEE_BASE_URL in ~/.cognee/.env")
        return 2

    state = probe_health(service_url, timeout=2.0)
    if state != "ready" and _is_local(service_url):
        _start_local_bootstrap(cwd)
        ready = _wait_for_health(service_url)
        if ready:
            service_url, api_key = resolved_http_endpoint_auth()  # key may have been minted
        else:
            _log(f"server at {service_url} still warming after {BOOT_WAIT_SECONDS:.0f}s; handing off anyway")
    elif state != "ready":
        _log(f"remote server {service_url} health={state}; handing off anyway")

    if not api_key:
        _log("no API key resolved (COGNEE_API_KEY or the local api_key.json cache); tools may return 401")

    prefix, extra_env = _find_cognee_mcp()
    os.environ.update(extra_env)
    if not prefix:
        _log("neither `cognee-mcp` nor `uvx` found on PATH; install uv (https://docs.astral.sh/uv/) or `pip install cognee-mcp`")
        return 3

    argv = prefix + ["--transport", "stdio", "--no-migration", "--api-url", service_url]
    if api_key:
        argv += ["--api-token", api_key]
    os.environ["COGNEE_BASE_URL"] = service_url
    if api_key:
        os.environ["COGNEE_API_KEY"] = api_key
    try:
        hook_log(
            "mcp_launch",
            {"service_url": service_url, "api_key_present": bool(api_key), "runner": prefix[0]},
        )
    except Exception:
        pass
    _log(f"exec {prefix[0]} -> {service_url}")
    os.execvp(argv[0], argv)
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
