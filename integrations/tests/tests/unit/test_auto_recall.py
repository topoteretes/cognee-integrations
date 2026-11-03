"""Auto recall: dataset routing on the server (SDK-1393).

Saves always go to the launch's ONE dataset, but graph recall defaults to
"auto": it names no dataset, so a cognee >= 1.6.6 server picks the readable
dataset(s) most likely to hold the answer when it can (SDK-1317).

Contract:
  * ``recall_via_http(auto_route=True)`` on a graph-only read with no session
    sends no ``datasets``/``dataset_ids`` and no session id, shared-memory ids
    included; a federated read set (COGNEE_PLUGIN_READ_DATASET_IDS), the code
    lane, a session-bound scope and ``auto_route=False`` keep their scope;
  * ``server_routes_datasets`` reads the version from /health, caches it per
    URL, keeps the last answer when a probe fails, and says False with none;
    only releases from 1.6.6 on route (1.6.6rc1 shipped without routing);
  * picking a dataset pins recall (``switch_launch_record``), ``set_recall_pinned``
    toggles it, a record switched before recall modes existed stays pinned, and
    ``auto_recall_active`` is False while pinned or under a federated read set;
  * the prompt hook passes ``auto_route`` to the graph scope only, and the
    /health read comes out of its recall budget;
  * ``shell_runtime_overrides`` tells the shell skills whether recall is auto.
"""

from __future__ import annotations

import io
import json
import time

import pytest
from utils.recall import drive_recall, load_lookup

URL = "http://cognee.test:8000"
WRITE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    monkeypatch.delenv("COGNEE_PLUGIN_READ_DATASET_IDS", raising=False)
    return common


@pytest.fixture
def sent(pc, monkeypatch):
    """Every /api/v1/recall payload ``recall_via_http`` sends."""
    payloads: list[dict] = []

    def fake(path, payload, timeout=None, **kw):
        payloads.append(dict(payload))
        return []

    monkeypatch.setattr(pc, "_json_http_request", fake)
    return payloads


def _graph(pc, **kw):
    args = {"session_id": "", "top_k": 5, "scope": ["graph"], "dataset": "agent_sessions"}
    args.update(kw)
    return pc.recall_via_http("what did we decide?", **args)


# ── the request ────────────────────────────────────────────────────────────


def test_auto_graph_recall_names_no_dataset(pc, sent):
    _graph(pc, auto_route=True)
    (payload,) = sent
    assert "datasets" not in payload and "dataset_ids" not in payload
    assert "session_id" not in payload
    assert payload["scope"] == ["graph"]


def test_without_auto_route_the_dataset_is_named(pc, sent):
    _graph(pc)
    assert sent[0]["datasets"] == ["agent_sessions"]


def test_auto_drops_shared_memory_ids(pc, sent):
    # Shared memory is on by default: keeping its ids would leave auto a no-op.
    _graph(pc, auto_route=True, dataset_ids=[WRITE_ID, OTHER_ID])
    (payload,) = sent
    assert "datasets" not in payload and "dataset_ids" not in payload


def test_pinned_recall_keeps_shared_memory_ids(pc, sent):
    _graph(pc, dataset_ids=[WRITE_ID, OTHER_ID])
    assert sent[0]["dataset_ids"] == [WRITE_ID, OTHER_ID]


def test_a_federated_read_set_keeps_its_scope(pc, sent, monkeypatch):
    monkeypatch.setenv("COGNEE_PLUGIN_READ_DATASET_IDS", json.dumps([WRITE_ID, OTHER_ID]))
    _graph(pc, auto_route=True)
    assert sent[0]["dataset_ids"] == [WRITE_ID, OTHER_ID]


def test_a_session_bound_scope_keeps_its_dataset(pc, sent):
    pc.recall_via_http(
        "q",
        session_id="s1",
        top_k=5,
        scope=["session_context"],
        dataset="agent_sessions",
        auto_route=True,
    )
    assert sent[0]["datasets"] == ["agent_sessions"]
    assert sent[0]["session_id"] == "s1"


def test_a_graph_recall_with_a_session_keeps_its_dataset(pc, sent):
    _graph(pc, session_id="s1", auto_route=True)
    assert sent[0]["datasets"] == ["agent_sessions"]


def test_the_code_lane_keeps_its_dataset(pc, sent):
    pc.recall_via_http(
        "q",
        session_id="",
        top_k=1,
        scope=["code"],
        dataset="repo-code",
        code_query={"operation": "query_facts", "file": "a.py"},
        auto_route=True,
    )
    assert sent[0]["datasets"] == ["repo-code"]


# ── the version gate ───────────────────────────────────────────────────────


@pytest.fixture
def health(pc, monkeypatch):
    """Answer GET /health with ``state["version"]`` (an Exception raises)."""
    state = {"version": "1.6.6", "calls": 0}

    def urlopen(url, timeout=None, context=None):
        state["calls"] += 1
        assert url == f"{URL}/health"
        if isinstance(state["version"], Exception):
            raise state["version"]
        body = json.dumps({"status": "ready", "health": "healthy", "version": state["version"]})

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Resp(body.encode())

    monkeypatch.setattr(pc.urllib.request, "urlopen", urlopen)
    return state


@pytest.mark.parametrize(
    ("version", "routes"),
    [
        ("1.6.6", True),
        ("1.6.6-local", True),
        ("1.6.6.post1", True),
        ("1.6.6+cloud", True),
        ("1.7.0.dev0", True),
        ("2.0.0", True),
        ("1.6.6.rc1", False),
        ("1.6.6rc1", False),
        ("1.6.6.dev0", False),
        ("1.6.6b2", False),
        ("1.6.5", False),
        ("1.6.5-local", False),
        ("1.6.5.dev0", False),
        ("0.5.3", False),
        ("unknown", False),
        ("1.6.6-custom", False),
        ("1.7.0-custom", False),
        ("", False),
    ],
)
def test_routing_starts_at_cognee_1_6_6(pc, health, version, routes):
    health["version"] = version
    assert pc.server_routes_datasets(URL) is routes


def test_the_answer_is_cached_per_server(pc, health):
    assert pc.server_routes_datasets(URL) is True
    health["version"] = "1.6.5"
    assert pc.server_routes_datasets(URL) is True  # cached
    assert health["calls"] == 1


def test_a_stale_cache_is_refreshed(pc, health, monkeypatch):
    assert pc.server_routes_datasets(URL) is True
    health["version"] = "1.6.5"
    later = pc.time.time() + pc._ROUTING_SUPPORT_TTL_SECONDS + 1
    monkeypatch.setattr(pc.time, "time", lambda: later)
    assert pc.server_routes_datasets(URL) is False
    assert health["calls"] == 2


def test_a_failed_probe_with_no_history_keeps_recall_scoped(pc, health):
    health["version"] = OSError("refused")
    assert pc.server_routes_datasets(URL) is False


def test_a_failed_probe_keeps_the_last_answer(pc, health, monkeypatch):
    assert pc.server_routes_datasets(URL) is True
    health["version"] = OSError("refused")
    later = pc.time.time() + pc._ROUTING_SUPPORT_TTL_SECONDS + 1
    monkeypatch.setattr(pc.time, "time", lambda: later)
    assert pc.server_routes_datasets(URL) is True


def _at(pc, monkeypatch, when):
    monkeypatch.setattr(pc.time, "time", lambda: when)


def test_a_failed_probe_with_no_history_is_retried_after_the_retry_interval(
    pc, health, monkeypatch
):
    start = pc.time.time()
    health["version"] = OSError("refused")
    assert pc.server_routes_datasets(URL) is False
    _at(pc, monkeypatch, start + pc._ROUTING_SUPPORT_RETRY_SECONDS - 1)
    assert pc.server_routes_datasets(URL) is False
    assert health["calls"] == 1  # not re-probed on every prompt
    health["version"] = "1.6.6"
    _at(pc, monkeypatch, start + pc._ROUTING_SUPPORT_RETRY_SECONDS + 1)
    assert pc.server_routes_datasets(URL) is True
    assert health["calls"] == 2


def test_a_failed_probe_after_a_success_is_retried_within_minutes(pc, health, monkeypatch):
    # The server is downgraded while it restarts: the probe that fails during the
    # restart must not keep the old answer for another hour.
    start = pc.time.time()
    assert pc.server_routes_datasets(URL) is True
    health["version"] = OSError("refused")
    failed_at = start + pc._ROUTING_SUPPORT_TTL_SECONDS + 1
    _at(pc, monkeypatch, failed_at)
    assert pc.server_routes_datasets(URL) is True
    health["version"] = "1.6.5"
    _at(pc, monkeypatch, failed_at + pc._ROUTING_SUPPORT_RETRY_SECONDS - 1)
    assert pc.server_routes_datasets(URL) is True
    assert health["calls"] == 2
    _at(pc, monkeypatch, failed_at + pc._ROUTING_SUPPORT_RETRY_SECONDS + 1)
    assert pc.server_routes_datasets(URL) is False
    assert health["calls"] == 3


def test_a_success_after_a_failed_probe_is_cached_for_the_ttl(pc, health, monkeypatch):
    start = pc.time.time()
    health["version"] = OSError("refused")
    assert pc.server_routes_datasets(URL) is False
    health["version"] = "1.6.6"
    recovered_at = start + pc._ROUTING_SUPPORT_RETRY_SECONDS + 1
    _at(pc, monkeypatch, recovered_at)
    assert pc.server_routes_datasets(URL) is True
    _at(pc, monkeypatch, recovered_at + pc._ROUTING_SUPPORT_RETRY_SECONDS + 1)
    assert pc.server_routes_datasets(URL) is True
    assert health["calls"] == 2


# ── "auto" is not a dataset name ───────────────────────────────────────────


@pytest.mark.parametrize("value", ["auto", "AUTO", " Auto "])
def test_an_env_dataset_of_auto_means_the_default_dataset(pc, monkeypatch, value):
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", value)
    monkeypatch.delenv("COGNEE_SESSION_KEY", raising=False)
    assert pc.resolve_active_dataset("") == "agent_sessions"
    assert pc.configured_dataset_name(value) == "agent_sessions"


def test_other_env_datasets_are_kept(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "autos")
    monkeypatch.delenv("COGNEE_SESSION_KEY", raising=False)
    assert pc.resolve_active_dataset("") == "autos"


@pytest.mark.parametrize("value", ["auto", " AUTO "])
def test_a_configured_dataset_of_auto_seeds_the_default(suite, isolated_modules, value):
    config = isolated_modules(suite, "config")
    assert config.get_dataset({"dataset": value}) == "agent_sessions"
    assert config.get_dataset({"dataset": "research"}) == "research"


# ── pinning ────────────────────────────────────────────────────────────────


@pytest.fixture
def launch(pc):
    key = "host-auto-1"
    pc._write_map_record(
        key, {"session_id": "s1", "conn_uuid": "c1", "dataset": "agent_sessions", "host_key": key}
    )
    return key


def test_recall_is_auto_until_a_dataset_is_picked(pc, launch, monkeypatch):
    monkeypatch.setattr(pc, "server_routes_datasets", lambda *a, **k: True)
    assert pc.recall_is_pinned(launch) is False
    assert pc.auto_recall_active(URL, launch) is True
    pc.switch_launch_record(launch, session_id="s2", dataset="research", conn_uuid="c2")
    assert pc.recall_is_pinned(launch) is True
    assert pc.auto_recall_active(URL, launch) is False


def test_set_recall_pinned_toggles_without_touching_the_dataset(pc, launch):
    pc.set_recall_pinned(launch, True)
    assert pc.recall_is_pinned(launch) is True
    rec = pc.set_recall_pinned(launch, False)
    assert pc.recall_is_pinned(launch) is False
    assert rec["dataset"] == "agent_sessions" and rec["session_id"] == "s1"


def test_a_record_switched_before_recall_modes_stays_pinned(pc, launch):
    rec = pc._read_map_record(launch)
    pc._write_map_record(launch, {**rec, "switched_at": "2026-10-01T00:00:00Z"})
    assert pc.recall_is_pinned(launch) is True
    pc.set_recall_pinned(launch, False)
    assert pc.recall_is_pinned(launch) is False


def test_a_federated_read_set_is_not_auto(pc, launch, monkeypatch):
    monkeypatch.setattr(pc, "server_routes_datasets", lambda *a, **k: True)
    monkeypatch.setenv("COGNEE_PLUGIN_READ_DATASET_IDS", json.dumps([WRITE_ID]))
    assert pc.auto_recall_active(URL, launch) is False


def test_an_old_server_keeps_recall_scoped(pc, launch, monkeypatch):
    monkeypatch.setattr(pc, "server_routes_datasets", lambda *a, **k: False)
    assert pc.auto_recall_active(URL, launch) is False


def test_the_shell_skills_learn_whether_recall_is_auto(pc, launch, monkeypatch):
    # The skills find their launch the way a shell tool does: COGNEE_SESSION_KEY.
    monkeypatch.setenv("COGNEE_SESSION_KEY", launch)
    monkeypatch.setattr(pc, "server_routes_datasets", lambda *a, **k: True)
    assert pc.shell_runtime_overrides(URL)["recall_auto"] == "1"
    pc.set_recall_pinned(launch, True)
    assert pc.shell_runtime_overrides(URL)["recall_auto"] == ""


# ── the prompt hook ────────────────────────────────────────────────────────


@pytest.fixture
def lookup(suite, hook_module, monkeypatch):
    module = load_lookup(suite, hook_module, monkeypatch)
    monkeypatch.setattr(module, "resolve_active_dataset_ids", lambda *a, **k: ("", []))
    monkeypatch.setattr(module, "get_dataset", lambda config: "agent_sessions")
    return module


@pytest.mark.parametrize("auto_route", [True, False])
def test_the_hook_routes_the_graph_scope_only(lookup, monkeypatch, auto_route):
    run = drive_recall(lookup, monkeypatch, auto_route=auto_route)
    assert run.kwargs["graph"]["auto_route"] is auto_route
    for scope, kwargs in run.kwargs.items():
        if scope != "graph":
            assert not kwargs.get("auto_route"), scope


def test_the_hook_routes_under_shared_memory(suite, hook_module, monkeypatch):
    module = load_lookup(suite, hook_module, monkeypatch)
    monkeypatch.setattr(
        module, "resolve_active_dataset_ids", lambda *a, **k: (WRITE_ID, [WRITE_ID, OTHER_ID])
    )
    monkeypatch.setattr(module, "get_dataset", lambda config: "agent_sessions")
    run = drive_recall(module, monkeypatch, auto_route=True)
    assert run.kwargs["graph"]["auto_route"] is True


def test_the_health_read_comes_out_of_the_recall_budget(lookup, monkeypatch):
    monkeypatch.setenv("COGNEE_RECALL_BUDGET", "2")

    def slow_probe(*a, **k):
        time.sleep(0.3)
        return True

    run = drive_recall(lookup, monkeypatch, auto_route=slow_probe)
    assert run.timeouts["graph"] <= 2 - 0.3


def test_a_probe_that_eats_the_budget_dispatches_nothing(lookup, monkeypatch):
    monkeypatch.setenv("COGNEE_RECALL_BUDGET", "0.3")

    def slow_probe(*a, **k):
        time.sleep(0.3)
        return True

    run = drive_recall(lookup, monkeypatch, auto_route=slow_probe)
    assert run.timeouts == {}
