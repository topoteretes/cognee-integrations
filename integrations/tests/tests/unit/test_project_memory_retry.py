"""Project memory provisioning retries a project-tag probe the server never answered.

``prepare()`` confirms, once per session, that the server stores project tags
(``node_set`` on typed entries) and whether a companion dataset is attested. A
spec probe the server never answered (timeout, connection error, non-JSON, 408,
429, 5xx) keeps the state pending with a ``retry_at`` on a growing backoff, and
``route()`` retries once it has passed; entries stay buffered meanwhile, so
nothing is written untagged. A spec without ``node_set``, or a refused spec (any
other HTTP status), stays a permanent error. Any companion failure falls back to
the primary dataset at once, as on main.

``route()`` never runs the first ``prepare()``: until session setup has run it
(or gave up and called ``allow_retry``), the state is pending with no
``retry_at`` and ``route()`` only raises, so a hook can't bind the session to a
key or dataset that is not final yet.
"""

from __future__ import annotations

import json
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
    [
        _timeout(),
        ConnectionRefusedError(),
        json.JSONDecodeError("Expecting value", "<html>", 0),
        urllib.error.HTTPError("https://tenant/openapi.json", 408, "timeout", {}, None),
        urllib.error.HTTPError("https://tenant/openapi.json", 503, "unavailable", {}, None),
    ],
    ids=["timeout", "refused", "non-json", "408", "503"],
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


def test_a_never_prepared_session_is_not_prepared_by_a_hook(env, monkeypatch):
    # Session setup has not run prepare() yet: pending, no retry_at. A drain
    # keeps the entries queued and sends nothing, not even a probe.
    pm, common = env
    pm.begin("primary", "s", "/tmp")
    assert "retry_at" not in _state(pm, common)
    server = _Server()
    monkeypatch.setattr(common, "_json_http_request", server)
    common.append_warmup_entry("primary", "s", {"type": "qa", "question": "q", "answer": "a"})
    assert common.drain_warmup_entries("primary", "s") == (0, 1)
    assert server.calls == []
    with pytest.raises(RuntimeError, match="pending"):
        pm.route("primary", "s")
    assert _state(pm, common)["pending"] is True


def test_a_hook_before_setup_does_not_bind_the_session_to_an_unminted_key(env, monkeypatch):
    pm, common = env
    monkeypatch.setattr(common, "resolved_http_endpoint_auth", lambda: ("https://tenant", ""))
    pm.begin("primary", "s", "/tmp")
    monkeypatch.setattr(common, "_json_http_request", _Server())
    with pytest.raises(RuntimeError, match="pending"):
        pm.route("primary", "s")  # a hook running before the key is minted

    monkeypatch.setattr(common, "resolved_http_endpoint_auth", lambda: ("https://tenant", "agent"))
    assert pm.prepare("primary", "s")["pending"] is False  # session setup, key now final
    assert pm.route("primary", "s")["node_set"] == ["project-fixed"]


def test_a_hook_before_setup_does_not_lose_the_companion_dataset(env, monkeypatch):
    pm, common = env
    monkeypatch.delenv("COGNEE_PROJECT_NODE_SET")
    monkeypatch.setenv("COGNEE_SESSION_COMPANION_DATASET", "true")
    pm.begin("primary", "s", "/tmp")
    datasets: list = []

    def server(path, *a, **k):
        if path == "/api/v1/datasets":
            return list(datasets)
        return {
            "permissions_verified": True,
            "primary_dataset_id": "id",
            "dataset_name": "primary-agent_sessions",
            "dataset_id": "cid",
        }

    monkeypatch.setattr(common, "_json_http_request", server)
    with pytest.raises(RuntimeError, match="pending"):
        pm.route("primary", "s")  # before session setup created the dataset

    datasets.append({"name": "primary", "id": "id"})
    pm.prepare("primary", "s")
    assert pm.route("primary", "s")["write"] == "primary-agent_sessions"
    assert "fallback" not in _state(pm, common)


def test_a_setup_that_gave_up_lets_a_later_hook_prepare(env, monkeypatch):
    pm, common = env
    pm.begin("primary", "s", "/tmp")
    pm.allow_retry("primary", "s")
    state = _state(pm, common)
    assert state["pending"] is True and "attempts" not in state
    assert pm.pending_notice("primary", "s") == ""  # nothing failed to answer yet
    server = _Server()
    monkeypatch.setattr(common, "_json_http_request", server)
    with pytest.raises(RuntimeError, match="pending"):
        pm.route("primary", "s")  # before retry_at
    assert server.calls == []

    _expire_retry(pm, common)
    common.append_warmup_entry("primary", "s", {"type": "qa", "question": "q", "answer": "a"})
    assert common.drain_warmup_entries("primary", "s") == (1, 0)
    assert server.writes()[0]["entry"]["node_set"] == ["project-fixed"]
    assert _state(pm, common)["pending"] is False


def test_allow_retry_leaves_a_prepared_or_backed_off_state_alone(env, monkeypatch):
    pm, common = env
    pm.begin("primary", "s", "/tmp")
    monkeypatch.setattr(common, "_json_http_request", _Server(fail=_timeout()))
    pm.prepare("primary", "s")
    backoff = _state(pm, common)["retry_at"]
    pm.allow_retry("primary", "s")
    assert _state(pm, common)["retry_at"] == backoff

    monkeypatch.setattr(common, "_json_http_request", _Server())
    _expire_retry(pm, common)
    pm.route("primary", "s")
    pm.allow_retry("primary", "s")
    assert "retry_at" not in _state(pm, common)


@pytest.mark.parametrize("code", [401, 403, 404])
def test_a_refused_spec_is_a_permanent_error(env, monkeypatch, code):
    pm, common = env
    pm.begin("primary", "s", "/tmp")
    refused = urllib.error.HTTPError("https://tenant/openapi.json", code, "no", {}, None)
    monkeypatch.setattr(common, "_json_http_request", _Server(fail=refused))
    state = pm.prepare("primary", "s")
    assert state["pending"] is False and "retry_at" not in state
    assert pm.pending_notice("primary", "s") == ""
    with pytest.raises(RuntimeError, match="could not be verified"):
        pm.route("primary", "s")


@pytest.mark.parametrize(
    "fail",
    [
        urllib.error.URLError(ConnectionRefusedError()),
        urllib.error.URLError(socket.timeout("timed out")),
        urllib.error.HTTPError("https://tenant", 503, "unavailable", {}, None),
        urllib.error.HTTPError("https://tenant", 403, "forbidden", {}, None),
    ],
    ids=["refused", "timeout", "503", "403"],
)
def test_any_companion_failure_falls_back_to_the_primary_at_once(env, monkeypatch, fail):
    # As on main: a companion-only session never waits on the server.
    pm, common = env
    monkeypatch.delenv("COGNEE_PROJECT_NODE_SET")
    monkeypatch.setenv("COGNEE_SESSION_COMPANION_DATASET", "true")
    pm.begin("primary", "s", "/tmp")

    def server(path, *a, **k):
        if path == "/api/v1/datasets":
            return [{"name": "primary", "id": "id"}]
        raise fail

    monkeypatch.setattr(common, "_json_http_request", server)
    state = pm.prepare("primary", "s")
    assert state["pending"] is False and "retry_at" not in state
    assert state["fallback"] in ("URLError", "HTTPError")
    assert pm.route("primary", "s")["write"] == "primary"
    assert pm.pending_notice("primary", "s") == ""


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
