"""The bridge's dedup + poll-budget state machine
(_plugin_common.persist_session_cache_to_graph_via_http).

Kept at the seam: what is under test is which SHA256 digests get marked
written, and that the poll deadline is an *overall* budget across documents —
decisions that are invisible on the wire. The submit itself is covered in
integration/test_bridge_post.py.

Contract:
  * a digest is marked written ONLY when the graph is confirmed queryable
    (completed) or genuinely unpollable (unknown / no dataset_id) — errored,
    timeout and parse_error stay unmarked so the detached retry re-submits;
  * an already-synced document is not re-posted;
  * one document failing must not abort the others.

Migrated from claude-code/tests/test_bridge_poll.py.
"""

from __future__ import annotations

import hashlib
import time

import pytest


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    if not suite.has_background_remember:
        pytest.skip(f"{suite.name}: the bridge submits synchronously and never polls")
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


@pytest.fixture
def run_bridge(pc, tmp_path, monkeypatch):
    """Drive persist_session_cache_to_graph_via_http with the HTTP seams mocked.

    Returns (wrote, written_state, calls); ``post_results`` returns a different
    result per POST call, in order, to exercise one document failing while
    another succeeds.
    """

    def _run(
        outcome,
        *,
        post_result=None,
        post_results=None,
        preseed_state=None,
        docs=("qa text", ""),
        wait_sleep=0.0,
    ):
        calls = {"post": 0, "wait": 0}
        written: dict = {}

        def _post(*a, **k):
            calls["post"] += 1
            if post_results is not None:
                return post_results[min(calls["post"] - 1, len(post_results) - 1)]
            return post_result or {"ok": True, "dataset_id": "d1", "pipeline_run_id": "p1"}

        def _wait(*a, **k):
            calls["wait"] += 1
            if wait_sleep:
                time.sleep(wait_sleep)
            return outcome

        monkeypatch.setattr(pc, "_local_api_url", lambda: "http://x")
        monkeypatch.setattr(pc, "_backend_reachable", lambda url: True)
        monkeypatch.setattr(pc, "_api_key", lambda: "k")
        monkeypatch.setattr(pc, "_format_cached_bridge_document", lambda dataset, sid: docs)
        monkeypatch.setattr(pc, "_bridge_file", lambda sid: tmp_path / "bridge.json")
        monkeypatch.setattr(
            pc,
            "_load_json_file",
            lambda p: {"_state": dict(preseed_state)} if preseed_state else {},
        )
        monkeypatch.setattr(pc, "_write_json_file", lambda p, data: written.update(data))
        monkeypatch.setattr(pc, "_post_remember_document", _post)
        monkeypatch.setattr(pc, "wait_for_cognify", _wait)

        wrote = pc.persist_session_cache_to_graph_via_http("ds", "sid")
        return wrote, written.get("_state", {}), calls

    return _run


def test_dedup_marks_only_on_completed(run_bridge):
    wrote, state, calls = run_bridge("completed")
    assert wrote is True
    assert len(state) == 1
    assert calls["wait"] == 1


def test_dedup_not_marked_on_errored(run_bridge):
    wrote, state, _ = run_bridge("errored")
    assert wrote is False
    assert state == {}


def test_dedup_not_marked_on_timeout(run_bridge):
    wrote, state, _ = run_bridge("timeout")
    assert wrote is False
    assert state == {}


def test_dedup_marked_on_unknown(run_bridge):
    """Genuinely unpollable (old server): mark it, or it re-syncs forever."""
    wrote, state, _ = run_bridge("unknown")
    assert wrote is True
    assert len(state) == 1


def test_no_dataset_id_marks_and_skips_poll(run_bridge):
    wrote, state, calls = run_bridge(
        "completed", post_result={"ok": True, "dataset_id": "", "pipeline_run_id": ""}
    )
    assert wrote is True
    assert len(state) == 1
    assert calls["wait"] == 0  # nothing to poll without a dataset_id


def test_parse_error_not_marked(run_bridge):
    # An unparseable 2xx must NOT be marked written (it's retried), unlike a
    # valid response with no dataset_id.
    wrote, state, calls = run_bridge(
        "completed", post_result={"ok": True, "dataset_id": "", "parse_error": True}
    )
    assert wrote is False
    assert state == {}
    assert calls["wait"] == 0


def test_already_synced_skips_post(pc, run_bridge):
    key = f"{pc._bridge_cache_key('ds', 'sid')}:qa"
    digest = hashlib.sha256("qa text".encode("utf-8")).hexdigest()
    wrote, state, calls = run_bridge("completed", preseed_state={key: digest})
    assert calls["post"] == 0  # unchanged document is not re-posted
    assert wrote is False


def test_post_failure_skips_document(run_bridge):
    # A failing POST leaves the digest unmarked (retried later), no crash.
    wrote, state, calls = run_bridge("completed", post_result={"ok": False, "status": 500})
    assert wrote is False
    assert state == {}
    assert calls["wait"] == 0  # never polled — the submit failed


def test_one_doc_fails_other_continues(run_bridge):
    wrote, state, calls = run_bridge(
        "completed",
        docs=("qa text", "trace text"),
        post_results=[
            {"ok": False, "status": 503},
            {"ok": True, "dataset_id": "d2", "pipeline_run_id": "p2"},
        ],
    )
    assert calls["post"] == 2  # both attempted; the first failure didn't abort the loop
    assert calls["wait"] == 1  # only the successful submit was polled
    assert wrote is True
    assert len(state) == 1  # only the trace document was marked written


def test_overall_deadline_across_documents(run_bridge, monkeypatch):
    # poll_deadline is an overall budget: once the first document's wait
    # exhausts it, the second is skipped (not given a fresh full deadline).
    monkeypatch.setenv("COGNEE_BRIDGE_POLL_DEADLINE", "0.01")
    wrote, state, calls = run_bridge("completed", docs=("qa text", "trace text"), wait_sleep=0.05)
    assert calls["post"] == 1  # second document skipped once the budget is spent
    assert calls["wait"] == 1
    assert len(state) == 1  # only the first document synced
    assert wrote is True
