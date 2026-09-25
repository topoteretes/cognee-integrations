"""HTTP 402 as a credits signal: the marker note and the hooks that write it.

The billing overview says what the balance IS; a 402 from a billable route
says the tenant could not pay for THIS request — the one positive exhaustion
signal the plugin sees, and the only one that works when the overview itself
cannot be fetched (a dev tenant asking the wrong platform host). The contract:

  * ``record_payment_required(op)`` stamps ``payment_required: {op, at}`` on the
    tenant's marker entry — by explicit tenant id, else by the entry bound to
    this service URL, else on a ``url:<service_url>`` placeholder;
  * ``clear_payment_required()`` removes the note, and removes a placeholder
    that held nothing else; with nothing to clear it does not even take the lock;
  * both no-op on a local server and never raise;
  * recall / trace+answer saves / remember / improve record a 402 under their
    own operation name and clear the note on success — any other failure
    leaves the note alone.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error

import pytest
from utils.recall import URL, arm_code_lane, drive_recall

_TENANT = "f8c21da4-6674-4cc5-bc56-de5e93db881d"
_URL = f"https://tenant-{_TENANT}.aws.cognee.ai"


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_BASE_URL", _URL)
    return common


@pytest.fixture
def events(pc, monkeypatch):
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(pc, "hook_log", lambda ev, detail=None: recorded.append((ev, detail or {})))
    return recorded


def _note(pc, key):
    entry = pc.read_credits_marker().get(key) or {}
    return entry.get("payment_required")


def _balance_entry(remaining=0.04):
    return {
        "remaining_usd": remaining,
        "spent_usd": 19.96,
        "total_usd": 20.0,
        "base_url": _URL,
        "tenant_id": _TENANT,
        "checked_at": 1_800_000_000.0,
    }


# ── record ─────────────────────────────────────────────────────────────────


def test_record_with_tenant_id_stamps_that_entry(pc, events):
    pc._write_credits_marker({_TENANT: _balance_entry()})
    pc.record_payment_required("recall", tenant_id=_TENANT)
    note = _note(pc, _TENANT)
    assert note["op"] == "recall"
    assert note["at"] > 0
    # The balance reading is untouched: the note rides beside it.
    assert pc.read_credits_marker()[_TENANT]["remaining_usd"] == 0.04
    assert ("credits_payment_required", {"op": "recall", "base_url": _URL}) in events


def test_record_without_tenant_id_finds_the_entry_bound_to_this_url(pc):
    pc._write_credits_marker({_TENANT: _balance_entry()})
    pc.record_payment_required("remember")
    assert _note(pc, _TENANT)["op"] == "remember"
    assert set(pc.read_credits_marker()) == {_TENANT}, "no placeholder when a binding exists"


def test_record_with_nothing_bound_parks_the_note_on_a_url_placeholder(pc):
    pc.record_payment_required("recall")
    key = pc._placeholder_credits_key(_URL)
    marker = pc.read_credits_marker()
    assert set(marker) == {key}
    assert marker[key]["base_url"] == _URL, "the renderer selects entries by base_url"
    assert marker[key]["payment_required"]["op"] == "recall"
    assert "remaining_usd" not in marker[key]


def test_record_creates_a_missing_tenant_entry_bound_to_this_url(pc):
    pc.record_payment_required("improve", tenant_id=_TENANT)
    entry = pc.read_credits_marker()[_TENANT]
    assert entry["base_url"] == _URL
    assert entry["payment_required"]["op"] == "improve"


def test_record_overwrites_an_older_note(pc):
    pc.record_payment_required("recall", tenant_id=_TENANT)
    pc.record_payment_required("save", tenant_id=_TENANT)
    assert _note(pc, _TENANT)["op"] == "save"


def test_record_leaves_other_tenants_alone(pc):
    other = "0b54dcbd-6b52-4b3e-a1dd-9d251e0f31bb"
    other_entry = dict(_balance_entry(9.5), base_url="https://tenant-other.aws.cognee.ai")
    pc._write_credits_marker({other: other_entry})
    pc.record_payment_required("recall", tenant_id=_TENANT)
    marker = pc.read_credits_marker()
    assert marker[other] == other_entry
    assert _TENANT in marker


def test_record_truncates_and_defaults_the_operation_name(pc):
    pc.record_payment_required("x" * 60, tenant_id=_TENANT)
    assert len(_note(pc, _TENANT)["op"]) == 24
    pc.record_payment_required("", tenant_id=_TENANT)
    assert _note(pc, _TENANT)["op"] == "operation"


def test_record_noops_on_a_local_server(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", "http://localhost:8011")
    pc.record_payment_required("recall", tenant_id=_TENANT)
    assert pc.read_credits_marker() == {}


def test_record_survives_an_unwritable_marker(pc, events, monkeypatch):
    def _boom(marker):
        raise OSError("disk full")

    monkeypatch.setattr(pc, "_write_credits_marker", _boom)
    pc.record_payment_required("recall", tenant_id=_TENANT)  # must not raise
    assert [e for e, _ in events] == ["credits_marker_write_failed"]


# ── clear ──────────────────────────────────────────────────────────────────


def test_clear_removes_the_note_and_keeps_the_balance(pc, events):
    pc._write_credits_marker({_TENANT: _balance_entry()})
    pc.record_payment_required("recall", tenant_id=_TENANT)
    pc.clear_payment_required(tenant_id=_TENANT)
    entry = pc.read_credits_marker()[_TENANT]
    assert "payment_required" not in entry
    assert entry["remaining_usd"] == 0.04
    assert ("credits_payment_cleared", {"op": "recall"}) in events


def test_clear_without_tenant_id_uses_the_url_binding(pc):
    pc._write_credits_marker({_TENANT: _balance_entry()})
    pc.record_payment_required("recall")
    pc.clear_payment_required()
    assert "payment_required" not in pc.read_credits_marker()[_TENANT]


def test_clear_removes_a_placeholder_that_held_nothing_else(pc):
    pc.record_payment_required("recall")
    pc.clear_payment_required()
    assert pc.read_credits_marker() == {}


def test_clear_with_nothing_to_clear_writes_nothing(pc, events, monkeypatch):
    """The steady state on every prompt: one small read, no lock, no write."""
    pc._write_credits_marker({_TENANT: _balance_entry()})
    before = pc._CREDITS_MARKER.read_bytes()
    monkeypatch.setattr(pc, "_try_acquire_credits_lock", lambda: pytest.fail("took the lock"))
    monkeypatch.setattr(pc, "_write_credits_marker", lambda m: pytest.fail("wrote the marker"))
    pc.clear_payment_required(tenant_id=_TENANT)
    pc.clear_payment_required()
    assert pc._CREDITS_MARKER.read_bytes() == before
    assert events == []


def test_clear_noops_on_a_local_server(pc, monkeypatch):
    pc._write_credits_marker(
        {_TENANT: dict(_balance_entry(), payment_required={"op": "x", "at": 1})}
    )
    monkeypatch.setenv("COGNEE_BASE_URL", "http://localhost:8011")
    pc.clear_payment_required(tenant_id=_TENANT)
    assert _note(pc, _TENANT) == {"op": "x", "at": 1}


def test_clear_survives_an_unwritable_marker(pc, events, monkeypatch):
    pc.record_payment_required("recall", tenant_id=_TENANT)
    events.clear()

    def _boom(marker):
        raise OSError("disk full")

    monkeypatch.setattr(pc, "_write_credits_marker", _boom)
    pc.clear_payment_required(tenant_id=_TENANT)  # must not raise
    assert [e for e, _ in events] == ["credits_marker_write_failed"]


# ── the recall hook ────────────────────────────────────────────────────────


@pytest.fixture
def lookup(suite, hook_module, monkeypatch):
    module = hook_module(suite, "session-context-lookup.py")
    calls: list[tuple] = []
    monkeypatch.setattr(
        module, "record_payment_required", lambda op, **k: calls.append(("record", op))
    )
    monkeypatch.setattr(module, "clear_payment_required", lambda **k: calls.append(("clear",)))
    return module, calls


def _raise(code: int):
    def _fn(_prompt, **_kw):
        raise urllib.error.HTTPError(URL, code, "boom", {}, None)

    return _fn


def test_recall_402_is_recorded_once_across_the_fan_out(lookup, monkeypatch):
    module, calls = lookup
    run = drive_recall(module, monkeypatch, recall=_raise(402))
    assert calls == [("record", "recall")]
    # It is a credits problem, not a credentials or server one.
    assert not run.fired("recall_auth_rejected")
    assert not run.fired("recall_server_down")
    assert all(state != "auth_failed" for state, _url, _d in run.writes), run.writes
    assert run.fired("recall_error")


def test_recall_success_clears_the_note(lookup, monkeypatch):
    module, calls = lookup
    drive_recall(module, monkeypatch, recall={})
    assert calls == [("clear",)]


def test_recall_402_wins_over_scopes_that_got_through(lookup, monkeypatch):
    """A mixed round still means the tenant could not pay for part of the recall."""
    module, calls = lookup

    arm_code_lane(monkeypatch)

    def _mixed(_prompt, **kw):
        if kw["scope"] == ["code"]:
            return []
        raise urllib.error.HTTPError(URL, 402, "boom", {}, None)

    drive_recall(module, monkeypatch, recall=_mixed)
    assert calls == [("record", "recall")]


@pytest.mark.parametrize("code", [401, 403, 429, 500, 503])
def test_other_recall_failures_touch_no_note(lookup, monkeypatch, code):
    module, calls = lookup
    drive_recall(module, monkeypatch, recall=_raise(code))
    assert calls == []


# ── the store hook (trace + answer saves) ──────────────────────────────────


@pytest.fixture
def store(suite, hook_module, monkeypatch):
    module = hook_module(suite, "store-to-session.py")
    calls: list[tuple] = []
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(module, "notify", lambda *a, **k: None)
    monkeypatch.setattr(module, "resolve_runtime_mode", lambda: {"mode": "http", "base_url": URL})
    monkeypatch.setattr(module, "_load_session", lambda: ("sid", "ds", "uid"))
    monkeypatch.setattr(module, "append_warmup_entry", lambda *a, **k: None)
    monkeypatch.setattr(module, "touch_activity", lambda: None)
    monkeypatch.setattr(module, "bump_turn_counter", lambda sid: (1, False))
    monkeypatch.setattr(module, "pop_pending_prompt", lambda sid, **k: {"prompt": "q"})
    monkeypatch.setattr(module, "bump_save_counter", lambda *a, **k: None)
    monkeypatch.setattr(module, "server_usable", lambda url="": True)
    monkeypatch.setattr(
        module, "record_payment_required", lambda op, **k: calls.append(("record", op))
    )
    monkeypatch.setattr(module, "clear_payment_required", lambda **k: calls.append(("clear",)))
    return module, calls


_TOOL = {"tool_name": "Read", "tool_input": {"file_path": "/x"}, "tool_output": "ok"}
_STOP = {"assistant_message": "done", "turn_id": "t1"}
_PATHS = [("_store_tool_call", _TOOL), ("_store_assistant_stop", _STOP)]


def _raising(code: int):
    def _boom(*a, **k):
        raise urllib.error.HTTPError(URL, code, "boom", hdrs=None, fp=None)

    return _boom


@pytest.mark.parametrize("run, payload", _PATHS)
def test_save_402_is_recorded_as_save(store, monkeypatch, run, payload):
    module, calls = store
    monkeypatch.setattr(module, "remember_entry_via_http", _raising(402))
    asyncio.run(getattr(module, run)(payload))
    assert calls == [("record", "save")]


@pytest.mark.parametrize("run, payload", _PATHS)
def test_stored_write_clears_the_note(store, monkeypatch, run, payload):
    module, calls = store
    monkeypatch.setattr(module, "remember_entry_via_http", lambda *a, **k: {"entry_id": "e"})
    asyncio.run(getattr(module, run)(payload))
    assert calls == [("clear",)]


@pytest.mark.parametrize("run, payload", _PATHS)
@pytest.mark.parametrize("code", [422, 503])
def test_other_save_failures_touch_no_note(store, monkeypatch, run, payload, code):
    module, calls = store
    monkeypatch.setattr(module, "remember_entry_via_http", _raising(code))
    asyncio.run(getattr(module, run)(payload))
    assert calls == []


# ── the remember skill ─────────────────────────────────────────────────────


@pytest.fixture
def remember(suite, isolated_modules, monkeypatch):
    # The skill module first, the common module second: each isolated load pops
    # every suite module from sys.modules, and main()'s lazy
    # ``from _plugin_common import ...`` must resolve to the copy stubbed here.
    module = isolated_modules(suite, "_remember_http")
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_BASE_URL", _URL)
    calls: list[tuple] = []
    monkeypatch.setattr(pc, "record_payment_required", lambda op, **k: calls.append(("record", op)))
    monkeypatch.setattr(pc, "clear_payment_required", lambda **k: calls.append(("clear",)))
    monkeypatch.setattr(pc, "refresh_credits", lambda *a, **k: calls.append(("refresh",)))
    return module, calls


def _run_remember(module, monkeypatch, capsys, result):
    monkeypatch.setattr(module, "do_remember", lambda *a, **k: result)
    module.main([_URL, "key", "content", "ds", "user"])
    return json.loads(capsys.readouterr().out.strip())


def test_remember_402_is_recorded_and_the_balance_still_refreshes(remember, monkeypatch, capsys):
    module, calls = remember
    out = _run_remember(module, monkeypatch, capsys, {"error": "HTTP 402", "status": 402})
    assert out["status"] == 402
    assert calls == [("record", "remember"), ("refresh",)]


def test_remember_success_clears_the_note(remember, monkeypatch, capsys):
    module, calls = remember
    _run_remember(module, monkeypatch, capsys, {"dataset_id": "d", "status": "ok"})
    assert calls == [("clear",), ("refresh",)]


def test_other_remember_failures_touch_no_note(remember, monkeypatch, capsys):
    module, calls = remember
    _run_remember(module, monkeypatch, capsys, {"error": "HTTP 500", "status": 500})
    assert calls == [("refresh",)]


# ── improve ────────────────────────────────────────────────────────────────


@pytest.fixture
def improve(pc, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(pc, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(pc, "record_payment_required", lambda op, **k: calls.append(("record", op)))
    monkeypatch.setattr(pc, "clear_payment_required", lambda **k: calls.append(("clear",)))
    monkeypatch.setattr(pc, "refresh_credits", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_local_api_url", lambda: "http://x")
    monkeypatch.setattr(pc, "_backend_reachable", lambda url: True)
    monkeypatch.setattr(pc, "drain_warmup_entries", lambda *a, **k: (0, 0))
    monkeypatch.setattr(pc, "ensure_dataset_via_http", lambda d: None)
    monkeypatch.setattr(pc, "_DRAIN_RETRY_PAUSE_SECONDS", 0.0)
    # Antigravity's improve still takes a per-session lock and polls a busy answer.
    if hasattr(pc, "_acquire_improve_session_lock"):
        monkeypatch.setattr(pc, "_acquire_improve_session_lock", lambda *a, **k: True)
    if hasattr(pc, "_release_improve_session_lock"):
        monkeypatch.setattr(pc, "_release_improve_session_lock", lambda *a, **k: None)

    def _run(result):
        monkeypatch.setattr(pc, "improve_session_via_http", lambda *a, **k: result)
        if hasattr(pc, "run_session_improve_detailed"):
            pc.run_session_improve_detailed("ds", "sid")
        else:
            pc.run_session_improve("ds", "sid")
        return calls

    return _run


def test_improve_402_is_recorded(improve):
    assert improve({"ok": False, "status": 402, "error": "HTTP 402: Payment Required"}) == [
        ("record", "improve")
    ]


def test_improve_success_clears_the_note(improve):
    assert improve({"ok": True, "result": {}}) == [("clear",)]


def test_other_improve_failures_touch_no_note(improve):
    assert improve({"ok": False, "status": 500, "error": "boom"}) == []
