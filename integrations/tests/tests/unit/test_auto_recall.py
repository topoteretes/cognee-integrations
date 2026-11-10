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
  * COGNEE_PLUGIN_RECALL_MODE (auto | pinned, default auto, an unknown value is
    auto) sets the configured mode; COGNEE_PLUGIN_DATASET never pins recall; the
    launch record's own ``recall_pinned`` (True or False) wins over the setting.
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
    monkeypatch.delenv("COGNEE_PLUGIN_RECALL_MODE", raising=False)
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
    # Shared memory is on by default: keeping its ids would leave auto a no-op.
    _graph(pc, auto_route=True, dataset_ids=[WRITE_ID, OTHER_ID])
    (payload,) = sent
    assert "datasets" not in payload and "dataset_ids" not in payload
    assert "session_id" not in payload
    assert payload["scope"] == ["graph"]


@pytest.mark.parametrize(
    ("kwargs", "read_set", "expected"),
    [
        pytest.param({"auto_route": False}, False, {"datasets": ["agent_sessions"]}, id="not-auto"),
        pytest.param({}, True, {"dataset_ids": [WRITE_ID, OTHER_ID]}, id="federated-read-set"),
        pytest.param(
            {"session_id": "s1", "scope": ["session_context"]},
            False,
            {"datasets": ["agent_sessions"], "session_id": "s1"},
            id="session-bound-scope",
        ),
        pytest.param(
            {"session_id": "s1"}, False, {"datasets": ["agent_sessions"]}, id="graph-with-session"
        ),
        pytest.param(
            {
                "top_k": 1,
                "scope": ["code"],
                "dataset": "repo-code",
                "code_query": {"operation": "query_facts", "file": "a.py"},
            },
            False,
            {"datasets": ["repo-code"]},
            id="code-lane",
        ),
    ],
)
def test_auto_does_not_apply(pc, sent, monkeypatch, kwargs, read_set, expected):
    if read_set:
        monkeypatch.setenv("COGNEE_PLUGIN_READ_DATASET_IDS", json.dumps([WRITE_ID, OTHER_ID]))
    _graph(pc, **{"auto_route": True, **kwargs})
    assert {key: sent[0].get(key) for key in expected} == expected


# ── "auto" is not a dataset name ───────────────────────────────────────────


def test_an_env_dataset_of_auto_means_the_default_dataset(pc, monkeypatch):
    monkeypatch.delenv("COGNEE_SESSION_KEY", raising=False)
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", " AUTO ")
    assert pc.resolve_active_dataset("") == "agent_sessions"
    assert pc.configured_dataset_name(" AUTO ") == "agent_sessions"
    # Only the reserved word: other names are kept.
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "autos")
    assert pc.resolve_active_dataset("") == "autos"


def test_an_env_dataset_of_auto_never_reaches_the_launch_record(
    suite, isolated_modules, pc, monkeypatch
):
    # SessionStart seeds the launch record from load_config()["dataset"], and the
    # record then wins over every later "auto" check: saves, shared memory and
    # agent registration would all name a dataset called "auto".
    config = isolated_modules(suite, "config")
    assert config.get_dataset({"dataset": " AUTO "}) == "agent_sessions"
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", " AUTO ")
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


def test_a_record_switched_before_recall_modes_stays_pinned(pc, launch, monkeypatch):
    monkeypatch.setenv("COGNEE_PLUGIN_RECALL_MODE", "auto")
    rec = pc._read_map_record(launch)
    pc._write_map_record(launch, {**rec, "switched_at": "2026-10-01T00:00:00Z"})
    assert pc.recall_is_pinned(launch) is True
    assert pc.recall_pin_reason(launch) == "dataset_picked"
    pc.set_recall_pinned(launch, False)
    assert pc.recall_is_pinned(launch) is False


def test_the_recall_mode_sends_no_request(pc, launch, monkeypatch):
    # Auto needs no server probe: an unreachable server still reads as auto.
    def urlopen(*a, **k):
        raise AssertionError("the recall mode must not reach the server")

    monkeypatch.setattr(pc.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("COGNEE_BASE_URL", "http://cognee.test:8000")
    assert pc.recall_mode(launch) == ("auto", "")
    assert pc.auto_recall_active(launch) is True


@pytest.mark.parametrize("mode, recall_auto", [("auto", "1"), ("pinned", "")])
def test_the_shell_skills_follow_the_configured_mode_without_a_launch(
    pc, monkeypatch, mode, recall_auto
):
    # A search run where no launch resolves (a plain terminal) still follows
    # COGNEE_PLUGIN_RECALL_MODE instead of falling back to a pinned search.
    for var in (
        "COGNEE_SESSION_KEY",
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_SESSION_ID",
        "CODEX_THREAD_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(pc, "resolve_host_key_outside_hook", lambda cwd="": ("", "not_found"))
    monkeypatch.setenv("COGNEE_PLUGIN_RECALL_MODE", mode)
    assert pc.shell_runtime_overrides()["recall_auto"] == recall_auto


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


# ── COGNEE_PLUGIN_RECALL_MODE ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "mode"),
    [
        (" PINNED ", "pinned"),
        ("", "auto"),
        ("pin", "auto"),
        ("auto", "auto"),
    ],
)
def test_the_configured_recall_mode(pc, launch, monkeypatch, value, mode):
    monkeypatch.setenv("COGNEE_PLUGIN_RECALL_MODE", value)
    assert pc.configured_recall_mode() == mode
    expected = ("pinned", "configured_pin") if mode == "pinned" else ("auto", "")
    assert pc.recall_mode(launch) == expected


def test_an_unknown_recall_mode_is_auto_and_logged_once(pc, monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(pc, "hook_log", lambda name, data=None, **k: events.append((name, data)))
    monkeypatch.setattr(pc, "_recall_mode_invalid_logged", False)
    monkeypatch.setenv("COGNEE_PLUGIN_RECALL_MODE", "sticky")
    assert pc.configured_recall_mode() == "auto"
    assert pc.configured_recall_mode() == "auto"
    assert events == [("recall_mode_invalid", {"value": "sticky"})]


def test_the_recall_mode_set_only_in_the_env_file_applies(
    suite, isolated_modules, pc, launch, monkeypatch, tmp_path
):
    import os

    ef = isolated_modules(suite, "_env_file")
    env = tmp_path / "cognee.env"
    env.write_text('COGNEE_PLUGIN_RECALL_MODE="pinned"\n', encoding="utf-8")
    monkeypatch.setenv("COGNEE_ENV_FILE", str(env))
    # Recorded so teardown removes what the loader injects; absent for the load.
    monkeypatch.setenv("COGNEE_PLUGIN_RECALL_MODE", "")
    os.environ.pop("COGNEE_PLUGIN_RECALL_MODE")
    monkeypatch.setattr(ef, "_loaded", False)
    ef.load_env_file()
    assert pc.recall_mode(launch) == ("pinned", "configured_pin")


def test_recall_mode_names_why_recall_is_not_auto(pc, launch, monkeypatch):
    assert pc.recall_mode(launch) == ("auto", "")
    monkeypatch.setenv("COGNEE_PLUGIN_RECALL_MODE", "pinned")
    assert pc.recall_mode(launch) == ("pinned", "configured_pin")
    pc.set_recall_pinned(launch, True)
    assert pc.recall_mode(launch) == ("pinned", "dataset_picked")


def test_a_read_set_is_reported_over_a_pin(pc, launch, monkeypatch):
    # The recall request sends the read set over any pin, so that is the reason.
    monkeypatch.setenv("COGNEE_PLUGIN_RECALL_MODE", "pinned")
    pc.set_recall_pinned(launch, True)
    monkeypatch.setenv("COGNEE_PLUGIN_READ_DATASET_IDS", json.dumps([WRITE_ID]))
    assert pc.recall_mode(launch) == ("pinned", "read_dataset_ids")
    assert pc.auto_recall_active(launch) is False
