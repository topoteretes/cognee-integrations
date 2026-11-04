"""Integration tests for _plugin_common.server_usable (#298).

``server_usable`` is the write-hook readiness check: a fresh ready marker
short-circuits; a stale marker triggers ONE bounded /health probe that
re-marks ready on success (so the marker stays fresh through long agent turns
and the warmup buffer stops filling against a healthy server); a failed probe
is memoized so a genuinely-down server costs one probe per backoff window,
not one per tool call.

Promoted from a stubbed-probe unit test: the probes are now real GET /health
requests counted by the mock server, and the ready marker / failure memo are
real files under the per-test HOME. That makes the probe-count claims — the
whole point of the memoization — hold end to end.

Migrated from claude-code/tests/test_server_usable.py.
"""

from __future__ import annotations

import json

import pytest

HEALTH = "/health"


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


def _probes(mock_server) -> int:
    return sum(1 for c in mock_server.calls if c["method"] == "GET" and c["path"] == HEALTH)


def test_fresh_hint_short_circuits(pc, mock_server):
    """A fresh ready marker returns True without any network probe."""
    pc.mark_server_ready(mock_server.url)
    assert pc.server_usable(mock_server.url) is True
    assert _probes(mock_server) == 0


def test_stale_hint_healthy_probe_refreshes_marker(pc, mock_server):
    """Stale marker + healthy server: one probe, marker re-marked, True."""
    assert pc.server_ready_hint(mock_server.url) is False  # nothing marked yet
    assert pc.server_usable(mock_server.url) is True
    assert _probes(mock_server) == 1
    # The refreshed marker is what keeps later write hooks off the probe path.
    assert pc.server_ready_hint(mock_server.url) is True


def test_failed_probe_returns_false_and_memoizes(pc, mock_server):
    """Stale marker + down server: False, and the failure memo suppresses
    re-probing inside the backoff window (one probe total, not two)."""
    mock_server.set_health_status(503)
    assert pc.server_usable(mock_server.url) is False
    assert _probes(mock_server) == 1
    assert pc._PROBE_FAIL_MEMO.exists()
    # Second call inside the backoff window: no new probe.
    assert pc.server_usable(mock_server.url) is False
    assert _probes(mock_server) == 1


def test_absent_server_is_not_usable(pc, closed_port_url):
    """A genuinely closed port (not just an unhealthy response) reads as down."""
    assert pc.server_usable(closed_port_url) is False
    assert pc._PROBE_FAIL_MEMO.exists()


def test_expired_memo_probes_again_and_recovers(pc, mock_server):
    """An expired failure memo probes again; success clears the memo."""
    mock_server.set_health_status(503)
    assert pc.server_usable(mock_server.url) is False
    assert _probes(mock_server) == 1

    # Age the memo past the backoff window, then bring the server up.
    memo = json.loads(pc._PROBE_FAIL_MEMO.read_text(encoding="utf-8"))
    memo["failed_at"] = memo["failed_at"] - pc._PROBE_FAIL_BACKOFF_SECONDS - 1
    pc._PROBE_FAIL_MEMO.write_text(json.dumps(memo), encoding="utf-8")
    mock_server.set_health_status(200)

    assert pc.server_usable(mock_server.url) is True
    assert _probes(mock_server) == 2
    assert not pc._PROBE_FAIL_MEMO.exists()


def test_corrupt_memo_fails_open_to_probe(pc, mock_server):
    """A garbage memo file must not wedge the check: it probes normally."""
    pc._PROBE_FAIL_MEMO.parent.mkdir(parents=True, exist_ok=True)
    pc._PROBE_FAIL_MEMO.write_text("not json", encoding="utf-8")
    assert pc.server_usable(mock_server.url) is True
    assert _probes(mock_server) == 1
