"""Auto recall: dataset routing on the server (SDK-1393).

Saves always go to the launch's ONE dataset, but graph recall defaults to
"auto": it names no dataset, so a cognee 1.6.6 server picks the readable
dataset(s) most likely to hold the answer (SDK-1317); an older server searches
every readable dataset. Nothing probes the server version.

Contract:
  * ``recall_via_http(auto_route=True)`` on a graph-only read with no session
    sends no ``datasets``/``dataset_ids`` and no session id, shared-memory ids
    included; a federated read set (COGNEE_PLUGIN_READ_DATASET_IDS), the code
    lane, a session-bound scope and ``auto_route=False`` keep their scope;
  * picking a dataset pins recall (``switch_launch_record``), ``set_recall_pinned``
    toggles it, a record switched before recall modes existed stays pinned, and
    ``auto_recall_active`` is False while pinned or under a federated read set;
  * the recall mode reads local state only: no request reaches the server;
  * the prompt hook passes ``auto_route`` to the graph scope only;
  * ``shell_runtime_overrides`` tells the shell skills whether recall is auto;
  * an explicitly configured non-default COGNEE_PLUGIN_DATASET pins recall
    (compared after sanitizing, as config.py does), but the launch record's own
    ``recall_pinned`` (True or False) wins over it.
"""

from __future__ import annotations

import json

import pytest
from utils.recall import drive_recall, load_lookup

WRITE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    monkeypatch.delenv("COGNEE_PLUGIN_READ_DATASET_IDS", raising=False)
    monkeypatch.delenv("COGNEE_PLUGIN_DATASET", raising=False)
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


@pytest.mark.parametrize("value", ["auto", " AUTO "])
def test_an_env_dataset_of_auto_never_reaches_the_launch_record(
    suite, isolated_modules, pc, monkeypatch, value
):
    # SessionStart seeds the launch record from load_config()["dataset"], and the
    # record then wins over every later "auto" check: saves, shared memory and
    # agent registration would all name a dataset called "auto".
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", value)
    cfg = config.load_config()
    assert cfg["dataset"] == "agent_sessions"
    pc.ensure_launch_record("host-auto", "/w", dataset=cfg["dataset"])
    monkeypatch.setenv("COGNEE_SESSION_KEY", "host-auto")
    assert pc.resolve_active_dataset("host-auto") == "agent_sessions"
    assert config.get_dataset(cfg) == "agent_sessions"


# ── pinning ────────────────────────────────────────────────────────────────


@pytest.fixture
def launch(pc):
    key = "host-auto-1"
    pc._write_map_record(
        key, {"session_id": "s1", "conn_uuid": "c1", "dataset": "agent_sessions", "host_key": key}
    )
    return key


def test_recall_is_auto_until_a_dataset_is_picked(pc, launch, monkeypatch):
    assert pc.recall_is_pinned(launch) is False
    assert pc.auto_recall_active(launch) is True
    pc.switch_launch_record(launch, session_id="s2", dataset="research", conn_uuid="c2")
    assert pc.recall_is_pinned(launch) is True
    assert pc.auto_recall_active(launch) is False


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
    monkeypatch.setenv("COGNEE_PLUGIN_READ_DATASET_IDS", json.dumps([WRITE_ID]))
    assert pc.auto_recall_active(launch) is False


def test_the_recall_mode_sends_no_request(pc, launch, monkeypatch):
    # Auto needs no server probe: an unreachable server still reads as auto.
    def urlopen(*a, **k):
        raise AssertionError("the recall mode must not reach the server")

    monkeypatch.setattr(pc.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("COGNEE_BASE_URL", "http://cognee.test:8000")
    assert pc.recall_mode(launch) == ("auto", "")
    assert pc.auto_recall_active(launch) is True


def test_the_shell_skills_learn_whether_recall_is_auto(pc, launch, monkeypatch):
    # The skills find their launch the way a shell tool does: COGNEE_SESSION_KEY.
    monkeypatch.setenv("COGNEE_SESSION_KEY", launch)
    assert pc.shell_runtime_overrides()["recall_auto"] == "1"
    pc.set_recall_pinned(launch, True)
    assert pc.shell_runtime_overrides()["recall_auto"] == ""


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


# ── a configured dataset pins recall ───────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "pinned"),
    [
        ("research", True),
        (" research ", True),
        ("autos", True),
        ("", False),
        ("agent_sessions", False),
        (" agent_sessions ", False),
        ("auto", False),
        (" AUTO ", False),
        # Names config.py sanitizes to the default dataset are the default.
        ("agent sessions", False),
        ("agent.sessions", False),
        (" agent  sessions ", False),
        ("agent sessions 2", True),
    ],
)
def test_an_explicitly_configured_non_default_dataset_pins_recall(
    pc, launch, monkeypatch, value, pinned
):
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", value)
    assert pc.recall_is_pinned(launch) is pinned
    assert pc.auto_recall_active(launch) is (not pinned)
    expected = ("pinned", "configured_dataset") if pinned else ("auto", "")
    assert pc.recall_mode(launch) == expected


def test_an_explicit_unpin_beats_a_configured_dataset(pc, launch, monkeypatch):
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "research")
    assert pc.recall_pin_reason(launch) == "configured_dataset"
    pc.set_recall_pinned(launch, False)
    assert pc.recall_is_pinned(launch) is False
    assert pc.auto_recall_active(launch) is True


def test_a_pick_beats_a_default_configuration(pc, launch, monkeypatch):
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "agent_sessions")
    pc.set_recall_pinned(launch, True)
    assert pc.recall_pin_reason(launch) == "dataset_picked"


def test_a_legacy_switch_is_a_pick_whatever_is_configured(pc, launch, monkeypatch):
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "research")
    rec = pc._read_map_record(launch)
    pc._write_map_record(launch, {**rec, "switched_at": "2026-10-01T00:00:00Z"})
    assert pc.recall_pin_reason(launch) == "dataset_picked"


def test_a_configured_dataset_pins_recall_outside_a_launch(pc, monkeypatch):
    monkeypatch.delenv("COGNEE_SESSION_KEY", raising=False)
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "research")
    assert pc.recall_pin_reason("") == "configured_dataset"


def test_recall_mode_names_why_recall_is_not_auto(pc, launch, monkeypatch):
    assert pc.recall_mode(launch) == ("auto", "")
    monkeypatch.setenv("COGNEE_PLUGIN_READ_DATASET_IDS", json.dumps([WRITE_ID]))
    assert pc.recall_mode(launch) == ("pinned", "read_dataset_ids")
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "research")
    assert pc.recall_mode(launch) == ("pinned", "configured_dataset")
    pc.set_recall_pinned(launch, True)
    assert pc.recall_mode(launch) == ("pinned", "dataset_picked")
