"""Project memory provisioning retries a probe the server never answered.

``prepare()`` confirms, once per session, that the server stores project tags
(``node_set`` on typed entries) and whether a companion dataset is attested. A
probe that raises before a spec was read (timeout, connection error, non-JSON)
keeps the state pending with a ``retry_at`` on a growing backoff, and
``route()`` retries once it has passed; entries stay buffered meanwhile, so
nothing is written untagged. A spec without ``node_set`` stays a permanent error,
and an explicit companion refusal still falls back to the primary dataset.
"""

from __future__ import annotations

import socket
import time
import urllib.error

import pytest

# Claude Code and Codex ship the retrying copy (SDK-1336 makes the tag the
# default in coding mode there).
RETRY_SUITES = {"claude-code", "codex"}

_SPEC = {
    "components": {
        "schemas": {name: {"properties": {"node_set": {}}} for name in ("QAEntry", "TraceEntry")}
    }
}


@pytest.fixture
def env(suite, isolated_modules, monkeypatch, tmp_path):
    if suite.name not in RETRY_SUITES:
        pytest.skip("the retrying project memory ships for Claude Code and Codex only")
    pm = isolated_modules(suite, "_project_memory")
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "resolved_http_endpoint_auth", lambda: ("https://tenant", "key"))
    monkeypatch.setattr(common, "_PLUGIN_DIR", tmp_path)
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(common, "_bridge_file", lambda sid="": tmp_path / "bridge.json")
    monkeypatch.setattr(common, "_DRAIN_LOCK", tmp_path / "drain.lock")
    monkeypatch.setattr(common, "_BUFFER_LOCK", tmp_path / "buffer.lock")
    monkeypatch.delenv("COGNEE_SESSION_COMPANION_DATASET", raising=False)
    monkeypatch.setenv("COGNEE_PROJECT_NODE_SET", "project-fixed")
    return pm, common


class _Server:
    """Records each request; ``fail`` makes the next ones raise."""

    def __init__(self, fail: BaseException | None = None):
        self.fail = fail
        self.calls: list = []

    def __call__(self, path, payload=None, **kwargs):
        self.calls.append((path, payload, kwargs.get("timeout")))
        if path == "/openapi.json":
            if self.fail is not None:
                raise self.fail
            return _SPEC
        return {"status": "session_stored"}

    def writes(self) -> list:
        return [payload for path, payload, _ in self.calls if path == "/api/v1/remember/entry"]


def _state(pm, common) -> dict:
    return common._load_json_file(pm._path("primary", "s"))


def _expire_retry(pm, common) -> None:
    state = _state(pm, common)
    state["retry_at"] = time.time() - 1
    common._write_json_file(pm._path("primary", "s"), state)


def _timeout() -> BaseException:
    return urllib.error.URLError(socket.timeout("timed out"))


def test_probe_timeout_leaves_the_state_pending_with_a_backoff(env, monkeypatch):
    pm, common = env
    pm.begin("primary", "s", "/tmp")
    monkeypatch.setattr(common, "_json_http_request", _Server(fail=_timeout()))
    before = time.time()
    state = pm.prepare("primary", "s")
    assert state["pending"] is True
    assert "error" not in state
    assert state["attempts"] == 1
    assert before + 29 <= state["retry_at"] <= time.time() + 31


@pytest.mark.parametrize(
    "fail",
    [_timeout(), ConnectionRefusedError(), ValueError("Expecting value")],
    ids=["timeout", "refused", "non-json"],
)
def test_any_unanswered_spec_probe_is_retried(env, monkeypatch, fail):
    pm, common = env
    pm.begin("primary", "s", "/tmp")
    monkeypatch.setattr(common, "_json_http_request", _Server(fail=fail))
    state = pm.prepare("primary", "s")
    assert state["pending"] is True and "error" not in state


def test_backoff_grows_then_settles_at_ten_minutes(env, monkeypatch):
    pm, common = env
    pm.begin("primary", "s", "/tmp")
    monkeypatch.setattr(common, "_json_http_request", _Server(fail=_timeout()))
    delays = []
    for _ in range(4):
        now = time.time()
        delays.append(round(pm.prepare("primary", "s")["retry_at"] - now, -1))
    assert delays == [30, 120, 600, 600]


def test_route_before_retry_at_raises_without_an_http_call(env, monkeypatch):
    pm, common = env
    pm.begin("primary", "s", "/tmp")
    server = _Server(fail=_timeout())
    monkeypatch.setattr(common, "_json_http_request", server)
    pm.prepare("primary", "s")
    server.calls.clear()
    with pytest.raises(RuntimeError, match="pending"):
        pm.route("primary", "s")
    with pytest.raises(RuntimeError, match="pending"):
        common.remember_entry_via_http("primary", "s", {"type": "qa", "question": "q"})
    assert server.calls == []


def test_timeout_then_success_drains_the_buffer_tagged_in_the_same_session(env, monkeypatch):
    pm, common = env
    pm.begin("primary", "s", "/tmp")
    server = _Server(fail=_timeout())
    monkeypatch.setattr(common, "_json_http_request", server)
    pm.prepare("primary", "s")
    common.append_warmup_entry("primary", "s", {"type": "qa", "question": "q", "answer": "a"})
    common.append_warmup_entry("primary", "s", {"type": "trace", "origin_function": "Bash"})

    # Still before retry_at: nothing leaves the buffer, nothing is sent.
    assert common.drain_warmup_entries("primary", "s") == (0, 2)
    assert server.writes() == []

    _expire_retry(pm, common)
    server.fail = None
    assert _state(pm, common)["pending"] is True

    assert common.drain_warmup_entries("primary", "s") == (2, 0)
    probes = [c for c in server.calls if c[0] == "/openapi.json"]
    assert probes[-1][2] == pm.ROUTE_PREPARE_TIMEOUT
    writes = server.writes()
    assert [w["entry"]["type"] for w in writes] == ["qa", "trace"]
    assert all(w["entry"]["node_set"] == ["project-fixed"] for w in writes)
    state = _state(pm, common)
    assert state["pending"] is False and "retry_at" not in state


def test_spec_without_node_set_is_still_a_permanent_error(env, monkeypatch):
    pm, common = env
    pm.begin("primary", "s", "/tmp")
    monkeypatch.setattr(common, "_json_http_request", lambda *a, **k: {"components": {}})
    state = pm.prepare("primary", "s")
    assert state["pending"] is False and "retry_at" not in state
    with pytest.raises(RuntimeError, match="does not support"):
        pm.route("primary", "s")


def test_cloud_session_left_pending_is_prepared_by_the_first_drain(env, monkeypatch):
    # Cloud registration failed before prepare() ran: pending, no retry_at.
    pm, common = env
    pm.begin("primary", "s", "/tmp")
    assert "retry_at" not in _state(pm, common)
    server = _Server()
    monkeypatch.setattr(common, "_json_http_request", server)
    common.append_warmup_entry("primary", "s", {"type": "qa", "question": "q", "answer": "a"})
    assert common.drain_warmup_entries("primary", "s") == (1, 0)
    assert server.writes()[0]["entry"]["node_set"] == ["project-fixed"]
    assert _state(pm, common)["pending"] is False


def test_companion_transport_failure_retries_but_refusal_falls_back(env, monkeypatch):
    pm, common = env
    monkeypatch.delenv("COGNEE_PROJECT_NODE_SET")
    monkeypatch.setenv("COGNEE_SESSION_COMPANION_DATASET", "true")
    pm.begin("primary", "s", "/tmp")

    def unreachable(path, *a, **k):
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(common, "_json_http_request", unreachable)
    state = pm.prepare("primary", "s")
    assert state["pending"] is True and "fallback" not in state

    def refused(path, *a, **k):
        if path.endswith("/datasets"):
            return [{"name": "primary", "id": "id"}]
        raise urllib.error.HTTPError("https://tenant", 403, "forbidden", {}, None)

    monkeypatch.setattr(common, "_json_http_request", refused)
    _expire_retry(pm, common)
    assert pm.route("primary", "s")["write"] == "primary"
    assert _state(pm, common)["fallback"] == "HTTPError"


def test_pending_notice_is_shown_once_while_still_pending(env, monkeypatch):
    pm, common = env
    pm.begin("primary", "s", "/tmp")
    # Nothing failed yet: no note.
    assert pm.pending_notice("primary", "s") == ""
    monkeypatch.setattr(common, "_json_http_request", _Server(fail=_timeout()))
    pm.prepare("primary", "s")
    assert pm.pending_notice("primary", "s") == pm.PENDING_NOTICE
    assert "memory is queued and will be sent once it can" in pm.PENDING_NOTICE
    assert pm.pending_notice("primary", "s") == ""
