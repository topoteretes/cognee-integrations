"""Unit tests for the session->graph improve bridge
(_plugin_common.improve_session_via_http and run_session_improve).

Confirms the improve contract:
  * the sync POSTs /api/v1/improve with {dataset_name, session_ids, run_in_background};
  * a 2xx submit counts as success (improve is idempotent server-side);
  * 404/405/422 marks the server improve-unsupported and falls back to the
    legacy full-document bridge;
  * warmup-buffered entries are drained before improve runs;
  * a dataset_id in the response triggers best-effort cognify+memify polling.

Run: python integrations/claude-code/tests/test_improve_sync.py (or via pytest).
"""

import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402


class _Resp:
    def __init__(self, body=b"{}", status=200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _with_seams(**overrides):
    """Return (saved, apply) helpers for attribute-patching pc seams."""
    saved = {k: getattr(pc, k) for k in overrides}
    for k, v in overrides.items():
        setattr(pc, k, v)
    return saved


def _restore(saved):
    for k, v in saved.items():
        setattr(pc, k, v)


def test_improve_posts_expected_json_payload():
    captured = {}
    orig = urllib.request.urlopen

    def _fake(req, timeout=None, context=None):
        captured["req"] = req
        captured["timeout"] = timeout
        return _Resp(b'{"status":"running"}')

    urllib.request.urlopen = _fake
    saved = _with_seams(_local_api_url=lambda: "http://x", _api_key=lambda: "k")
    try:
        res = pc.improve_session_via_http("ds", "sid")
    finally:
        _restore(saved)
        urllib.request.urlopen = orig

    assert res["ok"] is True
    assert captured["req"].full_url.endswith("/api/v1/improve")
    body = json.loads(captured["req"].data.decode("utf-8"))
    assert body == {"dataset_name": "ds", "session_ids": ["sid"], "run_in_background": True}
    # Distillation/agent-context run inside the request even in background
    # mode, so the submit timeout must be generous (default 180s).
    assert captured["timeout"] >= 60


def test_improve_404_marks_unsupported():
    marker_writes = {}
    orig = urllib.request.urlopen

    def _raise(req, timeout=None, context=None):
        raise urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)

    urllib.request.urlopen = _raise
    saved = _with_seams(
        _local_api_url=lambda: "http://x",
        _api_key=lambda: "k",
        _write_json_file=lambda p, data: marker_writes.update({str(p): data}),
    )
    try:
        res = pc.improve_session_via_http("ds", "sid")
    finally:
        _restore(saved)
        urllib.request.urlopen = orig

    assert res["ok"] is False
    assert res["unsupported"] is True
    assert any("improve-unsupported" in p for p in marker_writes)


def test_improve_network_error_is_graceful():
    orig = urllib.request.urlopen

    def _raise(req, timeout=None, context=None):
        raise urllib.error.URLError("connection refused")

    urllib.request.urlopen = _raise
    saved = _with_seams(_local_api_url=lambda: "http://x", _api_key=lambda: "k")
    try:
        res = pc.improve_session_via_http("ds", "sid")
    finally:
        _restore(saved)
        urllib.request.urlopen = orig

    assert res["ok"] is False
    assert res["status"] == 0
    assert "error" in res


def test_improve_polls_when_dataset_id_present():
    polled = []
    orig = urllib.request.urlopen

    def _fake(req, timeout=None, context=None):
        return _Resp(b'{"status":"running","dataset_id":"d1"}')

    def _wait(dataset_id, *, deadline_seconds, **kw):
        polled.append(kw.get("pipeline", "cognify_pipeline"))
        return "completed"

    urllib.request.urlopen = _fake
    saved = _with_seams(
        _local_api_url=lambda: "http://x",
        _api_key=lambda: "k",
        wait_for_cognify=_wait,
    )
    try:
        res = pc.improve_session_via_http("ds", "sid")
    finally:
        _restore(saved)
        urllib.request.urlopen = orig

    assert res["ok"] is True
    assert polled == ["cognify_pipeline", "memify_pipeline"]
    assert res["cognify_status"] == "completed"
    assert res["memify_status"] == "completed"


def _run_session_improve(improve_result, *, unsupported_marker=False, drain_results=None):
    """Drive run_session_improve with all seams mocked; return (result, calls).

    ``drain_results`` is an optional list of (drained, remaining) tuples returned
    per drain call, defaulting to a clean (0, 0).
    """
    calls = {"drain": 0, "improve": 0, "legacy": 0}

    def _drain(d, s):
        calls["drain"] += 1
        if drain_results:
            return drain_results[min(calls["drain"] - 1, len(drain_results) - 1)]
        return (0, 0)

    saved = _with_seams(
        _local_api_url=lambda: "http://x",
        _backend_reachable=lambda url: True,
        drain_warmup_entries=_drain,
        ensure_dataset_via_http=lambda d: None,
        improve_unsupported=lambda url: unsupported_marker,
        improve_session_via_http=lambda d, s, **k: (
            calls.__setitem__("improve", calls["improve"] + 1) or improve_result
        ),
        persist_session_cache_to_graph_via_http=lambda d, s: (
            calls.__setitem__("legacy", calls["legacy"] + 1) or True
        ),
        hook_log=lambda *a, **k: None,
        _DRAIN_RETRY_PAUSE_SECONDS=0.0,
    )
    try:
        wrote = pc.run_session_improve("ds", "sid")
    finally:
        _restore(saved)
    return wrote, calls


def test_run_session_improve_happy_path_drains_then_improves():
    wrote, calls = _run_session_improve({"ok": True})
    assert wrote is True
    assert calls == {"drain": 1, "improve": 1, "legacy": 0}


def test_run_session_improve_falls_back_when_unsupported_response():
    wrote, calls = _run_session_improve({"ok": False, "unsupported": True, "status": 404})
    assert wrote is True
    assert calls["improve"] == 1
    assert calls["legacy"] == 1


def test_run_session_improve_skips_improve_when_marker_set():
    wrote, calls = _run_session_improve({"ok": True}, unsupported_marker=True)
    assert wrote is True
    assert calls["improve"] == 0  # marker short-circuits straight to legacy
    assert calls["legacy"] == 1


def test_run_session_improve_error_returns_false_without_legacy():
    wrote, calls = _run_session_improve({"ok": False, "status": 500, "error": "boom"})
    assert wrote is False
    assert calls["legacy"] == 0  # a transient server error is not an unsupported server


def test_improve_lock_skip_reports_busy():
    # improve() returns {} when the per-session lock skips the run — the helper
    # must surface that as busy, never as success.
    orig = urllib.request.urlopen

    def _fake(req, timeout=None, context=None):
        return _Resp(b"{}")

    urllib.request.urlopen = _fake
    saved = _with_seams(_local_api_url=lambda: "http://x", _api_key=lambda: "k")
    try:
        res = pc.improve_session_via_http("ds", "sid")
    finally:
        _restore(saved)
        urllib.request.urlopen = orig

    assert res["ok"] is False
    assert res["busy"] is True


def test_run_session_improve_retries_busy_until_lock_frees():
    # A lock-skipped improve may have snapshotted the cache before the latest
    # turns — run_session_improve must re-submit until a run actually lands.
    import os

    results = [{"ok": False, "busy": True}, {"ok": False, "busy": True}, {"ok": True}]
    calls = {"improve": 0}

    def _improve(d, s, **k):
        calls["improve"] += 1
        return results[min(calls["improve"] - 1, len(results) - 1)]

    os.environ["COGNEE_IMPROVE_BUSY_RETRY_INTERVAL"] = "0.1"
    saved = _with_seams(
        _local_api_url=lambda: "http://x",
        _backend_reachable=lambda url: True,
        drain_warmup_entries=lambda d, s: (0, 0),
        improve_unsupported=lambda url: False,
        improve_session_via_http=_improve,
        hook_log=lambda *a, **k: None,
    )
    try:
        wrote = pc.run_session_improve("ds", "sid")
    finally:
        _restore(saved)
        os.environ.pop("COGNEE_IMPROVE_BUSY_RETRY_INTERVAL", None)

    assert wrote is True
    assert calls["improve"] == 3


def test_incomplete_drain_returns_false_but_improve_still_runs():
    # Undelivered warmup entries mean the improve persisted an incomplete
    # session: the improve must still run (partial persist beats none), but the
    # sync must report failure so the caller's retry loop re-drives it.
    wrote, calls = _run_session_improve({"ok": True}, drain_results=[(0, 3), (0, 3)])
    assert wrote is False
    assert calls["improve"] == 1  # improve ran despite the incomplete drain
    assert calls["drain"] == 2  # one in-place retry happened
    assert calls["legacy"] == 0


def test_drain_retry_recovers_and_sync_succeeds():
    # First drain fails on a blip, the in-place retry delivers the tail →
    # the sync is complete and reports success.
    wrote, calls = _run_session_improve({"ok": True}, drain_results=[(0, 3), (3, 0)])
    assert wrote is True
    assert calls["drain"] == 2
    assert calls["improve"] == 1


def test_clean_drain_skips_retry():
    wrote, calls = _run_session_improve({"ok": True}, drain_results=[(2, 0)])
    assert wrote is True
    assert calls["drain"] == 1  # nothing remaining → no retry


def test_run_session_improve_busy_deadline_gives_up():
    import os

    calls = {"improve": 0}

    def _always_busy(d, s, **k):
        calls["improve"] += 1
        return {"ok": False, "busy": True}

    os.environ["COGNEE_IMPROVE_BUSY_RETRY_INTERVAL"] = "0.1"
    os.environ["COGNEE_IMPROVE_BUSY_DEADLINE"] = "0.25"
    saved = _with_seams(
        _local_api_url=lambda: "http://x",
        _backend_reachable=lambda url: True,
        drain_warmup_entries=lambda d, s: (0, 0),
        improve_unsupported=lambda url: False,
        improve_session_via_http=_always_busy,
        hook_log=lambda *a, **k: None,
    )
    try:
        wrote = pc.run_session_improve("ds", "sid")
    finally:
        _restore(saved)
        os.environ.pop("COGNEE_IMPROVE_BUSY_RETRY_INTERVAL", None)
        os.environ.pop("COGNEE_IMPROVE_BUSY_DEADLINE", None)

    assert wrote is False  # still busy at deadline → reported as not-synced
    assert calls["improve"] >= 2  # at least one retry happened


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
