"""End-of-turn code-graph freshness, wired into the Stop hook.

A code graph that lags the agent's own edits is worse than no graph: a stale
fact is delivered with the same confidence as a fresh one and misdirects the
next turn. The Stop hook closes that gap by re-submitting an indexed repo when
the turn changed its working tree.

What these tests pin is the wiring and its failure containment, not the git
fingerprinting itself (that lives in integration/test_code_graph.py):

  * the pass runs on Stop and never on a per-tool-call event, because
    re-parsing a repo after every Edit would be pure waste;
  * a down or unconfigured server is skipped quietly, leaving the fingerprint
    stale so the next turn retries;
  * nothing the pass does can escape into the hook — a Stop hook that raises
    loses the turn's assistant message.
"""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def store(suite, hook_module):
    return hook_module(suite, "store-to-session.py")


@pytest.fixture
def spy(monkeypatch):
    """A stub _code_graph whose reingest_if_changed records its call."""
    calls: list[tuple] = []
    module = types.ModuleType("_code_graph")

    def _reingest(cwd, service_url, api_key, **_kw):
        calls.append((cwd, service_url, api_key))
        return {"changed": True, "submitted": True, "dataset": "codebase-proj"}

    module.reingest_if_changed = _reingest
    monkeypatch.setitem(sys.modules, "_code_graph", module)
    return calls


def _seams(store, monkeypatch, *, usable=True, base_url="https://cloud.example"):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        store, "hook_log", lambda event, detail=None: events.append((event, detail or {}))
    )
    monkeypatch.setattr(store, "notify", lambda *a, **k: None)
    monkeypatch.setattr(
        store, "resolve_runtime_mode", lambda: {"mode": "http", "base_url": base_url}
    )
    monkeypatch.setattr(store, "server_usable", lambda url="": usable)
    return events


def test_change_is_submitted_and_logged(store, monkeypatch, spy):
    events = _seams(store, monkeypatch)
    store._maybe_reingest_code_repo({"cwd": "/work/proj"})

    assert spy == [("/work/proj", "https://cloud.example", "")]
    assert any(name == "code_reingest_submitted" for name, _ in events)


def test_unchanged_tree_logs_but_submits_nothing(store, monkeypatch):
    events = _seams(store, monkeypatch)
    module = types.ModuleType("_code_graph")
    module.reingest_if_changed = lambda *a, **k: {"changed": False, "repo_root": "/work/proj"}
    monkeypatch.setitem(sys.modules, "_code_graph", module)

    store._maybe_reingest_code_repo({"cwd": "/work/proj"})
    assert any(name == "code_reingest_unchanged" for name, _ in events)


def test_unreachable_server_is_skipped_quietly(store, monkeypatch, spy):
    """The stale fingerprint is kept on purpose: the next Stop retries rather
    than treating this turn's edits as already indexed."""
    events = _seams(store, monkeypatch, usable=False)
    store._maybe_reingest_code_repo({"cwd": "/work/proj"})

    assert spy == []
    assert any(name == "code_reingest_skipped_server" for name, _ in events)


def test_no_service_url_does_nothing(store, monkeypatch, spy):
    _seams(store, monkeypatch, base_url="")
    store._maybe_reingest_code_repo({"cwd": "/work/proj"})
    assert spy == []


def test_failures_are_contained(store, monkeypatch):
    """A Stop hook that raises loses the turn's assistant message — the
    freshness pass must never be the thing that does that."""
    events = _seams(store, monkeypatch)
    module = types.ModuleType("_code_graph")

    def _boom(*_a, **_kw):
        raise RuntimeError("git exploded")

    module.reingest_if_changed = _boom
    monkeypatch.setitem(sys.modules, "_code_graph", module)

    store._maybe_reingest_code_repo({"cwd": "/work/proj"})  # must not raise
    assert any(name == "code_reingest_error" for name, _ in events)


def test_pass_is_wired_to_stop_only(store):
    """Per-tool-call re-parsing would be waste: enola re-parses the whole repo,
    while a turn's edits are only settled at Stop."""
    import inspect

    source = inspect.getsource(store.main)
    stop_branch, tool_branch = source.split("else:", 1)
    assert "_maybe_reingest_code_repo" in stop_branch
    assert "_maybe_reingest_code_repo" not in tool_branch
