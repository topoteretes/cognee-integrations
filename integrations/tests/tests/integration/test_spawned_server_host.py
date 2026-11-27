"""Where a server this plugin boots listens.

Every client of that server dials ``http://localhost``. uvicorn binds 127.0.0.1 unless
told otherwise, and Windows resolves ``localhost`` to ::1 first: a refused connect to
::1 takes about 2 s there before the client falls back to IPv4, so every request paid
it. On Windows the spawn hands uvicorn ``UVICORN_HOST=localhost``, and asyncio's
``create_server`` listens on every address the name resolves to (still loopback only).
Elsewhere the default stays: a refused connect fails at once, and a host that resolves
``localhost`` to ::1 with IPv6 disabled could not bind it. An operator's own
``UVICORN_HOST`` always wins.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess

import pytest

windows_only = pytest.mark.skipif(os.name != "nt", reason="the bind host is set on Windows only")


@pytest.fixture
def session_start(suite, hook_module, monkeypatch):
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setenv("COGNEE_PRESENCE_REPROBE_DELAY", "0.05")
    return module


def _spawn(session_start, closed_port_url, monkeypatch) -> dict:
    """Drive the boot point with the uvicorn spawn faked; return its ``argv`` and kwargs."""
    captured: dict = {}

    class _FakeProc:
        pid = 4242

        def poll(self):
            return None

    real_popen = subprocess.Popen

    def fake_popen(argv, **kwargs):
        if "uvicorn" not in [str(a) for a in argv]:
            return real_popen(argv, **kwargs)
        captured["argv"] = argv
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(session_start, "ensure_cognee_installed", lambda *a, **k: True)
    monkeypatch.setattr(session_start.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(session_start, "_health_ok", lambda *a, **k: "argv" in captured)
    monkeypatch.setattr(session_start, "write_server_pidfile", lambda *a, **k: None)
    monkeypatch.setattr(
        session_start, "server_presence", lambda *a, **k: (session_start.PRESENCE_ABSENT, {})
    )
    session_start._ensure_local_server_running({"base_url": closed_port_url}, health_timeout=2.0)
    assert "uvicorn" in captured["argv"]
    return captured


@windows_only
def test_spawned_server_listens_on_localhost(session_start, closed_port_url, monkeypatch):
    monkeypatch.delenv("UVICORN_HOST", raising=False)
    spawn = _spawn(session_start, closed_port_url, monkeypatch)
    assert spawn["env"]["UVICORN_HOST"] == "localhost"
    # Nothing on the command line overrides it.
    assert "--host" not in [str(a) for a in spawn["argv"]]


@pytest.mark.skipif(os.name == "nt", reason="the default bind host stays off Windows only")
def test_spawned_server_keeps_uvicorn_default_elsewhere(
    session_start, closed_port_url, monkeypatch
):
    monkeypatch.delenv("UVICORN_HOST", raising=False)
    spawn = _spawn(session_start, closed_port_url, monkeypatch)
    assert "UVICORN_HOST" not in spawn["env"]


def test_operator_uvicorn_host_wins(session_start, closed_port_url, monkeypatch):
    monkeypatch.setenv("UVICORN_HOST", "127.0.0.1")
    spawn = _spawn(session_start, closed_port_url, monkeypatch)
    assert spawn["env"]["UVICORN_HOST"] == "127.0.0.1"


@windows_only
def test_localhost_binds_every_loopback_address_it_resolves_to():
    """The asyncio behavior the Windows default relies on."""
    infos = socket.getaddrinfo("localhost", None, type=socket.SOCK_STREAM)
    resolved = {info[4][0] for info in infos}

    async def bound_addresses():
        loop = asyncio.get_running_loop()
        server = await loop.create_server(asyncio.Protocol, host="localhost", port=0)
        try:
            return {sock.getsockname()[0] for sock in server.sockets}
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(bound_addresses()) == resolved
