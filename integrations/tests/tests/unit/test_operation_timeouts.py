"""Per-operation client timeouts: COGNEE_REMEMBER_TIMEOUT, COGNEE_REGISTER_TIMEOUT
and COGNEE_REACHABLE_TIMEOUT.

Only recall was tunable; the explicit remember submit and the session register
call had hardcoded timeouts. Each now reads its own env var, falls back to the
historical value when unset or malformed, and an explicit caller timeout still
wins. Ported from #259 (reworking #167).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def remember(suite, isolated_modules):
    return isolated_modules(suite, "_remember_http")


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 120.0),
        ("", 120.0),
        ("7.5", 7.5),
        ("not-a-number", 120.0),
        ("0", 120.0),
        ("-5", 120.0),
        ("inf", 120.0),
        ("nan", 120.0),
    ],
)
def test_remember_timeout_reads_its_env_var(remember, monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("COGNEE_REMEMBER_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("COGNEE_REMEMBER_TIMEOUT", value)
    assert remember._remember_timeout() == expected


def test_remember_main_passes_the_timeout_and_keeps_its_arguments(remember, monkeypatch):
    """main() must keep forwarding file_path and dataset_id (added after #259)."""
    captured = {}

    def fake(*args, **kwargs):
        captured["args"], captured["kwargs"] = args, kwargs
        return {"ok": True}

    monkeypatch.setattr(remember, "do_remember", fake)
    monkeypatch.setenv("COGNEE_REMEMBER_TIMEOUT", "7")
    remember.main(["http://x", "key", "content", "ds", "ns", "/tmp/f.py", "ds-id"])

    assert captured["args"] == ("http://x", "key", "content", "ds", "ns")
    assert captured["kwargs"] == {"file_path": "/tmp/f.py", "dataset_id": "ds-id", "timeout": 7.0}


def _capture_register_timeout(pc, monkeypatch, **kwargs) -> float:
    seen = {}

    def fake(path, payload=None, *, method="GET", timeout=None, **_):
        seen["timeout"] = timeout
        return {}

    monkeypatch.setattr(pc, "_json_http_request", fake)
    pc.register_agent_via_http(agent_session_name="conn-1", **kwargs)
    return seen["timeout"]


def test_register_timeout_defaults_to_15(pc, monkeypatch):
    monkeypatch.delenv("COGNEE_REGISTER_TIMEOUT", raising=False)
    assert _capture_register_timeout(pc, monkeypatch) == 15.0


def test_register_timeout_env_override_and_malformed_value(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_REGISTER_TIMEOUT", "4")
    assert _capture_register_timeout(pc, monkeypatch) == 4.0
    monkeypatch.setenv("COGNEE_REGISTER_TIMEOUT", "soon")
    assert _capture_register_timeout(pc, monkeypatch) == 15.0


def test_explicit_register_timeout_wins_over_the_env(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_REGISTER_TIMEOUT", "4")
    assert _capture_register_timeout(pc, monkeypatch, timeout=2.0) == 2.0


@pytest.mark.parametrize("value", ["0", "-3", "inf", "nan"])
def test_register_timeout_ignores_non_positive_and_non_finite_values(pc, monkeypatch, value):
    """A socket timeout of 0 fails at once and inf/nan raise; fall back to 15."""
    monkeypatch.setenv("COGNEE_REGISTER_TIMEOUT", value)
    assert _capture_register_timeout(pc, monkeypatch) == 15.0


def _capture_reachable_timeout(pc, monkeypatch, **kwargs) -> float:
    seen = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(url, timeout=None, **_):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(pc.urllib.request, "urlopen", fake_urlopen)
    assert pc._backend_reachable("http://x", **kwargs) is True
    return seen["timeout"]


def test_reachable_timeout_defaults_to_2(pc, monkeypatch):
    monkeypatch.delenv("COGNEE_REACHABLE_TIMEOUT", raising=False)
    assert _capture_reachable_timeout(pc, monkeypatch) == 2.0


def test_reachable_timeout_env_override(pc, monkeypatch):
    # A tenant whose /health takes seconds must not have every sync skipped (#443).
    monkeypatch.setenv("COGNEE_REACHABLE_TIMEOUT", "8")
    assert _capture_reachable_timeout(pc, monkeypatch) == 8.0


@pytest.mark.parametrize("value", ["soon", "0", "-3", "inf", "nan"])
def test_reachable_timeout_ignores_malformed_values(pc, monkeypatch, value):
    monkeypatch.setenv("COGNEE_REACHABLE_TIMEOUT", value)
    assert _capture_reachable_timeout(pc, monkeypatch) == 2.0


def test_explicit_reachable_timeout_wins_over_the_env(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_REACHABLE_TIMEOUT", "8")
    assert _capture_reachable_timeout(pc, monkeypatch, timeout=0.5) == 0.5
