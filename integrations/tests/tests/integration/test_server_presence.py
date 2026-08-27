"""server_presence — the boot-point busy-vs-dead discriminator.

Regression suite for the 2026-08-13 incident: a server busy cognifying missed
one 2s health probe, was declared absent, and the boot point upgraded the venv
and ran migrations under it while it held the graph store's file lock. The
presence check exists so that a missed probe is never treated as absence:
absence must be positively proven (refused port, no live server pid, confirmed
by a delayed re-probe), while any evidence of a live server — a TCP listener,
a non-200 answer, a live spawned pid — vetoes installing or booting.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading

import pytest


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    # Keep the absence-confirming re-probe fast (read per call, not at import).
    monkeypatch.setenv("COGNEE_PRESENCE_REPROBE_DELAY", "0.05")
    return common


@pytest.fixture
def stalled_server_url():
    """A TCP listener that accepts connections but never answers HTTP —
    a saturated/wedged server as the probe sees it."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    accepted: list[socket.socket] = []

    def _accept_forever():
        while True:
            try:
                conn, _ = server.accept()
                accepted.append(conn)
            except OSError:
                return

    threading.Thread(target=_accept_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}"
    for conn in accepted:
        conn.close()
    server.close()


def _port_of(url: str) -> int:
    return int(url.rsplit(":", 1)[1])


def test_serving_server_is_ready(pc, mock_server):
    verdict, evidence = pc.server_presence(mock_server.url)
    assert verdict == pc.PRESENCE_READY
    assert evidence["http"] == "ready"


def test_stalled_server_is_busy_never_absent(pc, stalled_server_url):
    """The incident case: HTTP probe times out, but the listener proves a
    server exists. Must be BUSY (boot veto), not ABSENT (boot license)."""
    verdict, evidence = pc.server_presence(stalled_server_url, probe_timeout=0.2)
    assert verdict == pc.PRESENCE_BUSY
    assert evidence["http"] == "slow"
    assert evidence["tcp"] == "listening"


def test_erroring_server_is_busy_not_absent(pc, mock_server):
    """A 500 from /health means alive-but-unwell: never a boot license."""
    mock_server.set_health_status(500)
    verdict, evidence = pc.server_presence(mock_server.url)
    assert verdict == pc.PRESENCE_BUSY
    assert evidence["tcp"] == "listening"


def test_refused_port_is_absent_after_confirmation(pc, closed_port_url):
    verdict, evidence = pc.server_presence(closed_port_url, probe_timeout=0.2)
    assert verdict == pc.PRESENCE_ABSENT, evidence
    assert evidence["tcp"] == "refused"
    # Absence was confirmed by the delayed second probe, not assumed from one.
    assert "http_retry" in evidence


def test_quick_form_skips_the_confirming_reprobe(pc, closed_port_url):
    verdict, evidence = pc.server_presence(closed_port_url, probe_timeout=0.2, confirm_absent=False)
    assert verdict == pc.PRESENCE_ABSENT, evidence
    assert "http_retry" not in evidence


def test_remote_host_is_never_absent(pc):
    """No local evidence exists for remote hosts: a non-answering remote is
    UNKNOWN (degrade), never ABSENT (nothing can boot a remote host)."""
    verdict, _ = pc.server_presence("http://cognee-nonexistent.invalid:9", probe_timeout=0.2)
    assert verdict == pc.PRESENCE_UNKNOWN


def test_live_spawned_pid_vetoes_boot_before_port_bind(pc, closed_port_url, monkeypatch):
    """Between uvicorn spawn and port bind, the pidfile is the only evidence."""
    port = _port_of(closed_port_url)
    pc.write_server_pidfile(port, os.getpid())
    monkeypatch.setattr(pc, "_pid_looks_like_server", lambda pid: True)
    verdict, evidence = pc.server_presence(closed_port_url, probe_timeout=0.2, confirm_absent=False)
    assert verdict == pc.PRESENCE_BUSY
    assert evidence["pid"] == os.getpid()


def test_dead_pidfile_cannot_veto_and_is_reaped(pc, closed_port_url):
    dead_pid = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid.wait()
    port = _port_of(closed_port_url)
    pc.write_server_pidfile(port, dead_pid.pid)
    verdict, evidence = pc.server_presence(closed_port_url, probe_timeout=0.2, confirm_absent=False)
    assert verdict == pc.PRESENCE_ABSENT, evidence
    assert not pc._server_pidfile(port).exists()


def test_reused_pid_running_something_else_is_ignored(pc, closed_port_url, monkeypatch):
    """OS pid reuse: a live pid whose command line is not the server must not
    veto boots (a stale record vetoing forever would strand the plugin)."""
    port = _port_of(closed_port_url)
    pc.write_server_pidfile(port, os.getpid())
    monkeypatch.setattr(pc, "_pid_looks_like_server", lambda pid: False)
    verdict, evidence = pc.server_presence(closed_port_url, probe_timeout=0.2, confirm_absent=False)
    assert verdict == pc.PRESENCE_ABSENT, evidence
    assert not pc._server_pidfile(port).exists()


# --- The boot point itself ----------------------------------------------------


@pytest.fixture
def session_start(suite, hook_module, monkeypatch):
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setenv("COGNEE_PRESENCE_REPROBE_DELAY", "0.05")
    return module


def test_boot_point_refuses_install_over_busy_server(
    session_start, stalled_server_url, monkeypatch
):
    """THE regression test: a live-but-unresponsive server at a boot point must
    abort — never upgrade the venv (and so never run migrations) under it."""
    installs: list[int] = []
    monkeypatch.setattr(
        session_start, "ensure_cognee_installed", lambda *a, **k: installs.append(1) or True
    )
    with pytest.raises(RuntimeError, match="present but not serving"):
        session_start._ensure_local_server_running(
            {"base_url": stalled_server_url}, health_timeout=1.0
        )
    assert not installs


def test_boot_point_proceeds_when_positively_absent(session_start, closed_port_url, monkeypatch):
    """A genuinely absent server still licenses the install path (returning
    False there stops the flow before any uvicorn spawn)."""
    installs: list[int] = []
    monkeypatch.setattr(
        session_start, "ensure_cognee_installed", lambda *a, **k: installs.append(1) or False
    )
    with pytest.raises(RuntimeError, match="install/upgrade failed"):
        session_start._ensure_local_server_running(
            {"base_url": closed_port_url}, health_timeout=1.0
        )
    assert installs
