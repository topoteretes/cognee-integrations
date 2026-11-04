"""Unit tests for the cohesive recall client + circuit breaker (_cognee_client.py).

The breaker is file-based (each plugin hook is a short-lived process), so these
tests point COGNEE_PLUGIN_STATE_DIR at a temp dir and patch the transport
(`do_recall`) to drive each branch.

Breaker semantics under test (SDK-356):
  * state is keyed by base_url — one server's failures never open another's
  * failures only count inside a sliding window (no accumulation across days)
  * tripping consumes the counted failures (half-open: one post-cooldown
    failure starts a fresh count instead of instantly re-opening)
  * transient results (timeouts) are no-verdict: neither failure nor success
  * the trip reason (unreachable vs server_error) is recorded

Migrated from claude-code/tests/test_cognee_client.py, parametrized over all
registered suites (the client is identical; Codex and Antigravity gain this
coverage).
"""

from __future__ import annotations

import json
import time

import pytest

URL = "http://x"

UNREACHABLE = "UNREACHABLE"  # _recall_http's sentinel; a plain string, compared by ==


@pytest.fixture
def cc(suite, isolated_modules, tmp_path, monkeypatch):
    client = isolated_modules(suite, "_cognee_client")
    # _state_path() reads the env at call time, so setting it after import is
    # fine (setting before would be scrubbed by the isolated import).
    monkeypatch.setenv("COGNEE_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    return client


def _stub(monkeypatch, cc, value):
    """Make the transport return a fixed value (restored automatically)."""
    monkeypatch.setattr(cc, "do_recall", lambda *a, **k: value)


def test_closed_passes_results_through(cc, monkeypatch):
    _stub(monkeypatch, cc, [{"text": "hit"}])
    assert cc.recall(URL, "", "q", "", '["graph"]', "5") == [{"text": "hit"}]
    assert cc.breaker_open(URL)[0] is False


def test_opens_after_threshold_unreachable_then_short_circuits(cc, monkeypatch):
    _stub(monkeypatch, cc, UNREACHABLE)
    for _ in range(cc._THRESHOLD):
        cc.recall(URL, "", "q", "", '["graph"]', "5")
    is_open, retry = cc.breaker_open(URL)
    assert is_open and retry > 0
    assert cc.breaker_reason(URL) == "unreachable"

    # While open, recall must NOT call the transport and must surface a 503 envelope.
    def _boom(*a, **k):
        raise AssertionError("transport must not be called while breaker is open")

    monkeypatch.setattr(cc, "do_recall", _boom)
    out = cc.recall(URL, "", "q", "", '["graph"]', "5")
    assert isinstance(out, dict) and out["status"] == 503 and out["authoritative"] is False


def test_5xx_trips_breaker_with_server_error_reason(cc, monkeypatch):
    _stub(monkeypatch, cc, {"error": "boom", "status": 503, "authoritative": False})
    for _ in range(cc._THRESHOLD):
        cc.recall(URL, "", "q", "", '["graph"]', "5")
    assert cc.breaker_open(URL)[0] is True
    assert cc.breaker_reason(URL) == "server_error"


def test_auth_4xx_does_not_trip(cc, monkeypatch):
    _stub(monkeypatch, cc, {"error": "unauthorized", "status": 403, "authoritative": False})
    for _ in range(cc._THRESHOLD + 2):
        cc.recall(URL, "k", "q", "", '["graph"]', "5")
    assert cc.breaker_open(URL)[0] is False  # config problem, not a backend outage


def test_transient_timeout_is_no_verdict(cc, monkeypatch):
    """A timeout envelope neither trips the breaker nor clears prior failures."""
    for _ in range(cc._THRESHOLD - 1):
        cc.record_failure("x", service_url=URL)
    _stub(
        monkeypatch,
        cc,
        {"error": "recall slow: timed out", "status": 0, "authoritative": False, "transient": True},
    )
    for _ in range(cc._THRESHOLD + 2):
        cc.recall(URL, "", "q", "", '["graph"]', "5")
    # Still one failure short of the threshold: transients counted as nothing.
    assert cc.breaker_open(URL)[0] is False
    cc.record_failure("x", service_url=URL)
    assert cc.breaker_open(URL)[0] is True


def test_empty_list_is_success_not_failure(cc, monkeypatch):
    _stub(monkeypatch, cc, [])
    for _ in range(cc._THRESHOLD + 2):
        cc.recall(URL, "", "q", "", '["graph"]', "5")
    assert cc.breaker_open(URL)[0] is False


def test_failures_are_keyed_by_server(cc):
    """Cloud failures must not open the breaker for a local server (or the other suite's)."""
    for _ in range(cc._THRESHOLD):
        cc.record_failure("x", service_url="https://cloud.example")
    assert cc.breaker_open("https://cloud.example")[0] is True
    assert cc.breaker_open("http://localhost:8011")[0] is False
    # No-URL callers (doctor) still see the worst open entry.
    assert cc.breaker_open()[0] is True


def test_failures_outside_window_do_not_count(cc):
    """Five blips spread over days must not open the breaker."""
    now = 1_000_000.0
    step = cc._WINDOW + 1
    for i in range(cc._THRESHOLD):
        cc.record_failure("x", now=now + i * step, service_url=URL)
    assert cc.breaker_open(URL, now=now + cc._THRESHOLD * step)[0] is False


def test_half_open_after_cooldown(cc):
    """Tripping consumes the failures: one post-cooldown failure must not re-open."""
    now = 1000.0
    for _ in range(cc._THRESHOLD):
        cc.record_failure("x", now=now, service_url=URL)
    assert cc.breaker_open(URL, now=now)[0] is True
    after = now + cc._COOLDOWN + 1
    assert cc.breaker_open(URL, now=after)[0] is False
    cc.record_failure("x", now=after, service_url=URL)
    assert cc.breaker_open(URL, now=after)[0] is False  # fresh count, not instant re-open


def test_record_success_clears(cc):
    for _ in range(cc._THRESHOLD):
        cc.record_failure("x", service_url=URL)
    assert cc.breaker_open(URL)[0] is True
    cc.record_success(URL)
    assert cc.breaker_open(URL)[0] is False


def test_legacy_flat_schema_is_discarded(cc):
    """A pre-upgrade flat breaker file (machine-wide) must read as closed."""
    path = cc._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"failures": 99, "cooldown_until": time.time() + 3600}), encoding="utf-8"
    )
    assert cc.breaker_open(URL)[0] is False
    assert cc.breaker_open()[0] is False


def test_dataset_forwarded_to_transport(cc, monkeypatch):
    captured = {}

    def _capture(*a, **k):
        captured["dataset"] = a[6] if len(a) > 6 else k.get("dataset", "")
        return []

    monkeypatch.setattr(cc, "do_recall", _capture)
    cc.recall(URL, "", "q", "", '["graph"]', "5", "my_dataset")
    assert captured.get("dataset") == "my_dataset"


def test_search_skill_shares_the_hooks_breaker(suite):
    """cognee-search.sh must not redirect COGNEE_PLUGIN_STATE_DIR: that gave the
    skill a private breaker in the per-plugin dir while the hooks, doctor and
    status line used ~/.cognee-plugin/recall-breaker.json."""
    script = (suite.scripts_dir / "cognee-search.sh").read_text(encoding="utf-8")
    exports = [
        line
        for line in script.splitlines()
        if "COGNEE_PLUGIN_STATE_DIR=" in line and not line.lstrip().startswith("#")
    ]
    assert exports == [], exports


def test_default_breaker_path_is_the_shared_root(suite, isolated_modules, monkeypatch):
    client = isolated_modules(suite, "_cognee_client")
    monkeypatch.delenv("COGNEE_PLUGIN_STATE_DIR", raising=False)
    path = client._state_path()
    assert path.name == "recall-breaker.json"
    assert path.parent.name == ".cognee-plugin", path
