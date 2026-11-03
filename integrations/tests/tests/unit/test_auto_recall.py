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
  * ``shell_runtime_overrides`` tells the shell skills whether recall is auto;
  * an explicitly configured non-default COGNEE_PLUGIN_DATASET pins recall, but
    the launch record's own ``recall_pinned`` (True or False) wins over it;
  * each cache entry records why the last probe gave no routing answer
    (``timeout``, ``http_<status>``, ``no_version``, ``error``, ``old_version``);
  * SessionStart warms the cache with the reachability budget, beside its own
    connection work, and sends nothing while the cached answer is fresh.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
import time
import types

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
    monkeypatch.delenv("COGNEE_PLUGIN_DATASET", raising=False)
    monkeypatch.delenv("COGNEE_REACHABLE_TIMEOUT", raising=False)
    monkeypatch.delenv("COGNEE_LOCAL_API_URL", raising=False)
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
        ("1.6.6.post1-local", True),
        ("1.6.6-local+abc", True),
        ("1.6.6-1", True),
        ("1.6.6.0", True),
        ("1.7.0.dev0", True),
        ("1.7.0-custom", True),
        ("1.7", True),
        ("2.0", True),
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
        ("1.6", False),
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


def test_an_unhealthy_server_still_names_its_version(pc, monkeypatch):
    # cognee answers /health with 503 while a critical store is down, and the
    # body still carries the version: that is an answer, not a failed probe.
    def urlopen(url, timeout=None, context=None):
        body = json.dumps({"status": "not ready", "health": "unhealthy", "version": "1.6.6"})
        raise pc.urllib.error.HTTPError(
            url, 503, "Service Unavailable", {}, io.BytesIO(body.encode())
        )

    monkeypatch.setattr(pc.urllib.request, "urlopen", urlopen)
    assert pc.server_routes_datasets(URL) is True
    entry = json.loads(pc._ROUTING_SUPPORT_FILE.read_text())[pc._normalize_service_url(URL)]
    assert entry["probe_failed"] is False


def test_an_error_page_without_a_version_is_a_failed_probe(pc, monkeypatch):
    def urlopen(url, timeout=None, context=None):
        raise pc.urllib.error.HTTPError(url, 502, "Bad Gateway", {}, io.BytesIO(b"<html>"))

    monkeypatch.setattr(pc.urllib.request, "urlopen", urlopen)
    assert pc.server_routes_datasets(URL) is False


def test_a_hung_probe_is_cut_off_at_its_timeout(pc, monkeypatch):
    # urllib's timeout is per socket operation (and DNS has none): the read as a
    # whole must still end on time.
    release = threading.Event()

    def urlopen(url, timeout=None, context=None):
        release.wait(5)
        raise OSError("released")

    monkeypatch.setattr(pc.urllib.request, "urlopen", urlopen)
    started = time.monotonic()
    try:
        assert pc.server_routes_datasets(URL, timeout=0.2) is False
        assert time.monotonic() - started < 1.0
    finally:
        release.set()


@pytest.mark.parametrize("checked_at", ["soon", [1], 10**12])
def test_a_corrupt_or_future_cache_entry_is_probed_again(pc, health, checked_at):
    pc._ROUTING_SUPPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    pc._ROUTING_SUPPORT_FILE.write_text(
        json.dumps(
            {
                pc._normalize_service_url(URL): {
                    "version": "1.6.5",
                    "routes": False,
                    "checked_at": checked_at,
                    "probe_failed": False,
                }
            }
        )
    )
    assert pc.server_routes_datasets(URL) is True
    assert health["calls"] == 1


def test_the_shell_skills_keep_their_launch_state_when_the_mode_fails(pc, launch, monkeypatch):
    monkeypatch.setenv("COGNEE_SESSION_KEY", launch)

    def broken(*a, **k):
        raise ValueError("bad cache")

    monkeypatch.setattr(pc, "server_routes_datasets", broken)
    overrides = pc.shell_runtime_overrides(URL)
    assert overrides["recall_auto"] == ""
    assert overrides["session_id"]
    assert overrides["dataset"]


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
    # Windows' monotonic clock ticks ~15.6 ms, so a 0.3 s sleep can measure a hair short.
    assert run.timeouts["graph"] <= 2 - 0.25


def test_a_probe_that_eats_the_budget_dispatches_nothing(lookup, monkeypatch):
    monkeypatch.setenv("COGNEE_RECALL_BUDGET", "0.3")

    def slow_probe(*a, **k):
        time.sleep(0.3)
        return True

    run = drive_recall(lookup, monkeypatch, auto_route=slow_probe)
    assert run.timeouts == {}


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
    ],
)
def test_an_explicitly_configured_non_default_dataset_pins_recall(
    pc, launch, monkeypatch, value, pinned
):
    monkeypatch.setattr(pc, "server_routes_datasets", lambda *a, **k: True)
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", value)
    assert pc.recall_is_pinned(launch) is pinned
    assert pc.auto_recall_active(URL, launch) is (not pinned)
    expected = ("pinned", "configured_dataset") if pinned else ("auto", "")
    assert pc.recall_mode(URL, launch) == expected


def test_an_explicit_unpin_beats_a_configured_dataset(pc, launch, monkeypatch):
    monkeypatch.setattr(pc, "server_routes_datasets", lambda *a, **k: True)
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "research")
    assert pc.recall_pin_reason(launch) == "configured_dataset"
    pc.set_recall_pinned(launch, False)
    assert pc.recall_is_pinned(launch) is False
    assert pc.auto_recall_active(URL, launch) is True


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
    monkeypatch.setattr(pc, "server_routes_datasets", lambda *a, **k: False)
    assert pc.recall_mode(URL, launch) == ("pinned", "server_cannot_route")
    monkeypatch.setenv("COGNEE_PLUGIN_READ_DATASET_IDS", json.dumps([WRITE_ID]))
    assert pc.recall_mode(URL, launch) == ("pinned", "read_dataset_ids")


# ── why routing is off ─────────────────────────────────────────────────────


@pytest.fixture
def logged(pc, monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        pc, "hook_log", lambda event, detail=None, *a, **k: events.append((event, detail or {}))
    )
    return events


def _entry(pc) -> dict:
    return json.loads(pc._ROUTING_SUPPORT_FILE.read_text())[pc._normalize_service_url(URL)]


def _probe_failures(events) -> list[dict]:
    return [d for e, d in events if e == "dataset_routing_probe_failed"]


def test_a_routing_server_records_no_reason(pc, health, logged):
    assert pc.server_routes_datasets(URL) is True
    assert _entry(pc)["reason"] == ""
    assert _probe_failures(logged) == []


def test_an_old_server_is_recorded_without_a_log_line(pc, health, logged):
    health["version"] = "1.6.5"
    assert pc.server_routes_datasets(URL) is False
    assert _entry(pc)["reason"] == "old_version"
    assert _probe_failures(logged) == []


def test_a_health_answer_without_a_version_is_recorded(pc, health, logged):
    health["version"] = ""
    assert pc.server_routes_datasets(URL) is False
    assert _entry(pc)["reason"] == "no_version"
    assert [d["reason"] for d in _probe_failures(logged)] == ["no_version"]


def test_a_refused_probe_is_recorded_as_an_error(pc, health, logged):
    health["version"] = OSError("refused")
    assert pc.server_routes_datasets(URL) is False
    assert _entry(pc)["reason"] == "error"
    assert [d["reason"] for d in _probe_failures(logged)] == ["error"]


def test_an_error_page_records_its_status(pc, monkeypatch, logged):
    def urlopen(url, timeout=None, context=None):
        raise pc.urllib.error.HTTPError(url, 502, "Bad Gateway", {}, io.BytesIO(b"<html>"))

    monkeypatch.setattr(pc.urllib.request, "urlopen", urlopen)
    assert pc.server_routes_datasets(URL) is False
    assert _entry(pc)["reason"] == "http_502"
    assert [d["reason"] for d in _probe_failures(logged)] == ["http_502"]


def test_a_slow_health_is_recorded_as_a_timeout(pc, monkeypatch, logged):
    release = threading.Event()

    def urlopen(url, timeout=None, context=None):
        release.wait(5)
        raise OSError("released")

    monkeypatch.setattr(pc.urllib.request, "urlopen", urlopen)
    try:
        assert pc.server_routes_datasets(URL, timeout=0.2) is False
    finally:
        release.set()
    entry = _entry(pc)
    assert entry["reason"] == "timeout" and entry["probe_failed"] is True
    assert [d["reason"] for d in _probe_failures(logged)] == ["timeout"]


def test_a_socket_timeout_is_a_timeout(pc, monkeypatch, logged):
    def urlopen(url, timeout=None, context=None):
        raise pc.urllib.error.URLError(pc.socket.timeout("timed out"))

    monkeypatch.setattr(pc.urllib.request, "urlopen", urlopen)
    assert pc.server_routes_datasets(URL) is False
    assert _entry(pc)["reason"] == "timeout"


def test_a_failed_probe_keeps_the_answer_and_says_why(pc, health, monkeypatch, logged):
    assert pc.server_routes_datasets(URL) is True
    health["version"] = OSError("refused")
    _at(pc, monkeypatch, pc.time.time() + pc._ROUTING_SUPPORT_TTL_SECONDS + 1)
    assert pc.server_routes_datasets(URL) is True
    entry = _entry(pc)
    assert entry["routes"] is True and entry["probe_failed"] is True
    assert entry["reason"] == "error" and entry["version"] == "1.6.6"


# ── the SessionStart warm-up ───────────────────────────────────────────────


def test_the_warmup_reads_with_the_reachability_budget(pc, monkeypatch):
    seen: list[tuple[str, float]] = []
    monkeypatch.setattr(
        pc, "server_routes_datasets", lambda url="", timeout=1.0: seen.append((url, timeout))
    )
    pc.finish_dataset_routing_warmup(pc.warm_dataset_routing())
    monkeypatch.setenv("COGNEE_REACHABLE_TIMEOUT", "7.5")
    pc.finish_dataset_routing_warmup(pc.warm_dataset_routing())
    # "" is the URL recall uses (COGNEE_LOCAL_API_URL / COGNEE_BASE_URL / default).
    assert seen == [("", pc.REACHABLE_TIMEOUT_DEFAULT_SECONDS), ("", 7.5)]


def test_the_warmup_never_raises(pc, monkeypatch):
    def broken(*a, **k):
        raise ValueError("bad cache")

    monkeypatch.setattr(pc, "server_routes_datasets", broken)
    pc.finish_dataset_routing_warmup(pc.warm_dataset_routing())
    pc.finish_dataset_routing_warmup(None)


def test_a_fresh_cache_costs_the_warmup_no_request(pc, health, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", URL)
    assert pc.server_routes_datasets(URL) is True
    pc.finish_dataset_routing_warmup(pc.warm_dataset_routing())
    assert health["calls"] == 1
    _at(pc, monkeypatch, pc.time.time() + pc._ROUTING_SUPPORT_TTL_SECONDS + 1)
    pc.finish_dataset_routing_warmup(pc.warm_dataset_routing())
    assert health["calls"] == 2


@pytest.fixture
def heavy(suite, hook_module, monkeypatch, tmp_path):
    """session-start's ``_run_heavy`` with every step but the routing warm-up stubbed."""
    for name in (
        "COGNEE_PLUGIN_DATASET",
        "COGNEE_REACHABLE_TIMEOUT",
        "COGNEE_LOCAL_API_URL",
        "COGNEE_PLUGIN_READ_DATASET_IDS",
    ):
        monkeypatch.delenv(name, raising=False)
    module = hook_module(suite, "session-start.py")
    common = sys.modules["_plugin_common"]
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(module, "_reexec_into_venv", lambda: None)

    async def ready(config):
        return None

    monkeypatch.setattr(module, "ensure_cognee_ready", ready)
    monkeypatch.setattr(module, "is_cloud_mode", lambda config: False)
    monkeypatch.setattr(module, "_resolve_agent_name", lambda *a, **k: "agent")
    monkeypatch.setattr(module, "probe_health", lambda *a, **k: "ready")
    monkeypatch.setattr(module, "write_connection_state", lambda *a, **k: None)
    monkeypatch.setitem(
        sys.modules, "_project_memory", types.SimpleNamespace(prepare=lambda *a, **k: {})
    )
    monkeypatch.setitem(
        sys.modules,
        "_code_graph",
        types.SimpleNamespace(autoindex_on_session_start=lambda *a, **k: None),
    )

    def run(url: str):
        monkeypatch.setenv("COGNEE_BASE_URL", url)
        return asyncio.run(
            module._run_heavy(
                {"base_url": url},
                str(tmp_path),
                "s1",
                "c1",
                "host-heavy",
                "agent_sessions",
                managed_endpoint=True,
                boot_timeout=1.0,
            )
        )

    run.common = common
    return run


def test_session_start_warms_the_routing_cache_with_the_reachability_budget(heavy, monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(
        heavy.common, "server_routes_datasets", lambda url="", timeout=1.0: seen.append(timeout)
    )
    monkeypatch.setenv("COGNEE_REACHABLE_TIMEOUT", "6")
    assert heavy(URL)[2] is True
    assert seen == [6.0]


def test_session_start_leaves_the_routing_answer_for_the_prompt_hook(heavy, mock_server):
    mock_server.set_server_version("1.6.6")
    assert heavy(mock_server.url)[2] is True
    common = heavy.common
    entry = json.loads(common._ROUTING_SUPPORT_FILE.read_text())[mock_server.url]
    assert entry["routes"] is True and entry["reason"] == ""
    probes = len([c for c in mock_server.calls if c["path"] == "/health"])
    # The prompt hook's short probe is not needed: the answer is cached.
    assert common.server_routes_datasets(mock_server.url, timeout=0.01) is True
    assert len([c for c in mock_server.calls if c["path"] == "/health"]) == probes == 1


def test_session_start_with_a_fresh_cache_sends_no_probe(heavy, mock_server):
    mock_server.set_server_version("1.6.6")
    common = heavy.common
    common._ROUTING_SUPPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    common._ROUTING_SUPPORT_FILE.write_text(
        json.dumps(
            {
                mock_server.url: {
                    "version": "1.6.6",
                    "routes": True,
                    "checked_at": time.time(),
                    "probe_failed": False,
                }
            }
        )
    )
    assert heavy(mock_server.url)[2] is True
    assert not [c for c in mock_server.calls if c["path"] == "/health"]


def test_a_failing_warmup_never_fails_session_start(heavy, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("no threads")

    monkeypatch.setattr(heavy.common, "warm_dataset_routing", broken)
    assert heavy(URL)[2] is True
