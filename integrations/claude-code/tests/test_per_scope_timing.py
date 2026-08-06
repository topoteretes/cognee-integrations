"""Unit tests for per-scope recall instrumentation (session-context-lookup.py).

Recall fans out over 4 scopes (session/trace/session_context/graph).
These tests drive the hook's ``_run`` in cloud mode with a fake ``recall_via_http``
and assert the emitted recall event carries a per-scope ``{hits, elapsed_ms}``
breakdown for all 4 scopes — including scopes that return nothing or never run —
without changing the existing aggregate ``counts``.

Run: python integrations/claude-code/tests/test_per_scope_timing.py (or via pytest).
"""

import asyncio
import importlib.util
import os
import pathlib
import sys
import tempfile
import types

# Pin the loop-guard so importing the plugin never re-execs into its venv.
os.environ.setdefault("COGNEE_PLUGIN_IN_VENV", "1")

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

SCOPES = ["session", "trace", "session_context", "graph"]


def _load_hook_module():
    """Import the hyphenated hook script under a clean module name."""
    path = SCRIPTS / "session-context-lookup.py"
    spec = importlib.util.spec_from_file_location("session_context_lookup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drive_run(recall_map, *, breaker=(False, 0), recall_fn=None):
    """Drive _run in cloud mode with a fake recall; return (output, events).

    Stubs the module's I/O helpers on a freshly imported hook module, injects a
    fake _cognee_client breaker, and points last_recall.json / recall-audit.log
    writes at a throwaway HOME. Global state (sys.modules, os.environ) is
    restored so the suite stays order-independent under pytest.
    """
    mod = _load_hook_module()
    events = []

    mod.hook_log = lambda ev, detail=None: events.append((ev, detail or {}))
    mod.notify = lambda *a, **k: None
    mod.resolve_runtime_mode = lambda: {"mode": "http", "base_url": "https://cloud.example"}
    mod.server_ready_hint = lambda _url: True
    mod._load_session_id = lambda: "sess-123"
    mod.read_and_reset_save_counter = lambda _sid: {"prompt": 0, "trace": 0, "answer": 0}
    mod.recall_via_http = recall_fn or (
        lambda prompt, **kw: list(recall_map.get(kw["scope"][0], []))
    )

    # The breaker is imported lazily inside _run (cloud mode); inject a fake so
    # the test is deterministic regardless of any on-disk breaker state.
    # breaker_open is keyed by service_url since SDK-356; record_success /
    # record_failure are the hot-path accounting hooks (no-ops here).
    fake_client = types.ModuleType("_cognee_client")
    fake_client.breaker_open = lambda service_url="": breaker
    fake_client.record_success = lambda service_url="": None
    fake_client.record_failure = lambda *a, **k: None
    saved_client = sys.modules.get("_cognee_client")
    sys.modules["_cognee_client"] = fake_client

    saved_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        os.environ["USERPROFILE"] = home
        try:
            output = asyncio.run(mod._run("please recall something relevant"))
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            if saved_client is None:
                sys.modules.pop("_cognee_client", None)
            else:
                sys.modules["_cognee_client"] = saved_client
    return output, events


def _event(events, name):
    for ev, detail in events:
        if ev == name:
            return detail
    return None


def _assert_valid_per_scope(per_scope):
    assert list(per_scope.keys()) == SCOPES, f"expected all 4 scopes in order, got {per_scope}"
    for label, rec in per_scope.items():
        assert isinstance(rec["hits"], int), f"{label} hits not int: {rec}"
        assert isinstance(rec["elapsed_ms"], (int, float)), f"{label} elapsed not numeric: {rec}"
        assert rec["elapsed_ms"] >= 0


def test_per_scope_on_hit():
    """A hit emits context_lookup_hit carrying per-scope hits + timing."""
    recall_map = {
        "session": [{"question": "q1", "answer": "a1"}],
        "trace": [],
        "graph": [{"source": "graph", "content": "gg"}],
        "session_context": [],
    }
    _out, events = _drive_run(recall_map)

    detail = _event(events, "context_lookup_hit")
    assert detail is not None, "expected a context_lookup_hit event"
    assert "counts" in detail, "existing aggregate counts must be preserved"
    per_scope = detail["per_scope"]
    _assert_valid_per_scope(per_scope)
    # Raw per-scope hit attribution (pre-bucketing): graph is NOT folded here.
    assert per_scope["session"]["hits"] == 1
    assert per_scope["trace"]["hits"] == 0
    assert per_scope["graph"]["hits"] == 1
    assert per_scope["session_context"]["hits"] == 0
    # Bucketing folds graph results into the stable graph_context counter.
    assert detail["counts"]["graph_context"] == 1


def test_per_scope_on_empty():
    """A total miss still emits per-scope timing for all 4 scopes (all ran)."""
    _out, events = _drive_run({s: [] for s in SCOPES})

    detail = _event(events, "context_lookup_empty")
    assert detail is not None, "expected a context_lookup_empty event"
    per_scope = detail["per_scope"]
    _assert_valid_per_scope(per_scope)
    assert all(rec["hits"] == 0 for rec in per_scope.values())
    # Every scope ran, so none carries the skipped marker.
    assert not any(rec.get("skipped") for rec in per_scope.values())


def test_all_scopes_skipped_when_breaker_open():
    """Breaker-open runs no scope, yet all 4 are still reported as skipped."""
    _out, events = _drive_run({s: [] for s in SCOPES}, breaker=(True, 30))

    detail = _event(events, "context_lookup_empty")
    assert detail is not None, "expected a context_lookup_empty event"
    per_scope = detail["per_scope"]
    _assert_valid_per_scope(per_scope)
    assert all(rec.get("skipped") for rec in per_scope.values())
    assert all(rec["hits"] == 0 and rec["elapsed_ms"] == 0 for rec in per_scope.values())


def test_scope_timeout_clamped_to_remaining_budget():
    """Per-scope timeouts shrink to the remaining budget; exhausted budget skips.

    With budget=0.8s and timeout=0.5s, a slow ``session`` (0.45s) leaves ~0.35s
    for ``trace`` — its dispatched timeout must be clamped below the full 0.5s.
    A slow ``trace`` (0.3s) then leaves less than MIN_SCOPE_TIMEOUT, so the
    remaining scopes are skipped and recall_budget_exceeded fires —
    previously each scope could run a full timeout past the deadline.
    """
    import time as _time

    dispatched = {}
    sleeps = {"session": 0.45, "trace": 0.3}

    def _slow_recall(prompt, **kw):
        scope = kw["scope"][0]
        dispatched[scope] = kw["timeout"]
        _time.sleep(sleeps.get(scope, 0))
        return []

    saved = {k: os.environ.get(k) for k in ("COGNEE_RECALL_TIMEOUT", "COGNEE_RECALL_BUDGET")}
    os.environ["COGNEE_RECALL_TIMEOUT"] = "0.5"
    os.environ["COGNEE_RECALL_BUDGET"] = "0.8"
    try:
        _out, events = _drive_run({}, recall_fn=_slow_recall)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # First scope had the whole budget: full per-call timeout.
    assert dispatched["session"] == 0.5
    # Second scope was clamped to the ~0.35s remaining (never the full 0.5).
    assert 0.2 <= dispatched["trace"] < 0.45, f"expected clamped timeout, got {dispatched}"
    # Budget exhausted before the remaining scopes: skipped, never dispatched.
    assert set(dispatched) == {"session", "trace"}
    assert _event(events, "recall_budget_exceeded") is not None
    per_scope = _event(events, "context_lookup_empty")["per_scope"]
    _assert_valid_per_scope(per_scope)
    for scope in ("session_context", "graph"):
        assert per_scope[scope].get("skipped"), f"{scope} should have been skipped"


def test_lookup_does_not_drain_warmup_buffer():
    """#298: the synchronous prompt-path hook must never replay the warmup
    buffer (10-30s stall); the async sibling (store-user-prompt) owns the
    drain. Pin: the hook module does not even import drain_warmup_entries."""
    mod = _load_hook_module()
    assert not hasattr(mod, "drain_warmup_entries")


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
