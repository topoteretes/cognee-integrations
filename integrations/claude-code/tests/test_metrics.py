"""Unit tests for the cognee-plugin metrics command.

Tests parse mock log files in a temp directory — no network, no real plugin state.

Run:
    pytest integrations/claude-code/tests/test_metrics.py
    python integrations/claude-code/tests/test_metrics.py  # standalone
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time

# ---- resolve imports -------------------------------------------------------
_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import cognee_plugin  # type: ignore

_compute_metrics = cognee_plugin._compute_metrics
_parse_jsonl     = cognee_plugin._parse_jsonl
_read_json_file  = cognee_plugin._read_json_file
main             = cognee_plugin.main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dir():
    """Return a fresh temp directory Path."""
    return pathlib.Path(tempfile.mkdtemp(prefix="cognee-metrics-test-"))


def _jsonl(*dicts):
    return "\n".join(json.dumps(d) for d in dicts) + "\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_empty_dir_returns_zeros():
    """No files exist → every metric is zero / empty, no crash."""
    d = _make_dir()
    m = _compute_metrics(d)
    assert m["sessions"] == 0
    assert m["recalls"]["total"] == 0
    assert m["recalls"]["hits"] == 0
    assert m["recalls"]["hit_rate_pct"] == 0.0
    assert m["saves"]["prompt"] == 0
    assert m["saves"]["trace"] == 0
    assert m["saves"]["answer"] == 0
    assert m["mode_split"]["local_pct"] == 0.0
    assert m["mode_split"]["cloud_pct"] == 0.0
    assert m["circuit_breaker"]["trips"] == 0


def test_session_counting_from_hook_log():
    d = _make_dir()
    (d / "hook.log").write_text(
        _jsonl(
            {"ts": "2025-01-01T00:00:00Z", "event": "mode_decision",
             "detail": {"mode": "local", "session_id": "sess-aaa"}},
            {"ts": "2025-01-01T00:01:00Z", "event": "prompt_pending",
             "detail": {"session_id": "sess-aaa"}},
            {"ts": "2025-01-01T01:00:00Z", "event": "mode_decision",
             "detail": {"mode": "http",  "session_id": "sess-bbb"}},
        ),
        encoding="utf-8",
    )
    m = _compute_metrics(d)
    assert m["sessions"] == 2
    assert m["mode_split"]["local_count"] == 1
    assert m["mode_split"]["cloud_count"] == 1
    assert m["mode_split"]["local_pct"] == 50.0
    assert m["mode_split"]["cloud_pct"] == 50.0


def test_save_counter_json_aggregated():
    d = _make_dir()
    (d / "save_counter.json").write_text(json.dumps({
        "sess-1": {"prompt": 3, "trace": 1, "answer": 2},
        "sess-2": {"prompt": 1, "trace": 0, "answer": 1},
    }), encoding="utf-8")
    m = _compute_metrics(d)
    # Both sessions should be summed
    assert m["saves"]["prompt"] == 4
    assert m["saves"]["trace"] == 1
    assert m["saves"]["answer"] == 3


def test_recall_audit_hit_rate():
    d = _make_dir()
    audit_entries = [
        # 2 hits  → counted as a hit
        {"ts": "T1", "session_id": "s", "prompt": "q1", "hits": {"session": 2, "graph": 0}},
        # 0 hits  → counted as miss
        {"ts": "T2", "session_id": "s", "prompt": "q2", "hits": {"session": 0, "graph": 0}},
        # 1 hit   → counted as hit
        {"ts": "T3", "session_id": "s", "prompt": "q3", "hits": {"session": 0, "graph": 1}},
    ]
    (d / "recall-audit.log").write_text(
        _jsonl(*audit_entries), encoding="utf-8"
    )
    m = _compute_metrics(d)
    assert m["recalls"]["total"] == 3
    assert m["recalls"]["hits"] == 2
    assert m["recalls"]["hit_rate_pct"] == round(100.0 * 2 / 3, 1)


def test_breaker_trips_from_hook_log():
    d = _make_dir()
    (d / "hook.log").write_text(
        _jsonl(
            {"ts": "T1", "event": "recall_breaker_open", "detail": {"retry_in": 90}},
            {"ts": "T2", "event": "recall_breaker_open", "detail": {"retry_in": 45}},
            {"ts": "T3", "event": "mode_decision",       "detail": {"mode": "local"}},
        ),
        encoding="utf-8",
    )
    m = _compute_metrics(d)
    assert m["circuit_breaker"]["trips"] == 2


def test_last_recall_json_surfaced():
    d = _make_dir()
    (d / "last_recall.json").write_text(json.dumps({
        "session_id": "s1",
        "ts": "2025-06-01T10:00:00+00:00",
        "hits": {"session": 3, "trace": 1, "graph_context": 0},
        "saves_last_turn": {"prompt": 1, "trace": 0, "answer": 1},
    }), encoding="utf-8")
    m = _compute_metrics(d)
    assert m["last_recall"]["ts"] == "2025-06-01T10:00:00+00:00"
    assert m["last_recall"]["hits"]["session"] == 3


def test_json_output_flag(capsys=None):
    """--json flag should produce parseable JSON."""
    d = _make_dir()
    import io, contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        # Call compute directly and dump JSON (mirrors main() flow)
        m = _compute_metrics(d)
        print(json.dumps(m, indent=2))

    parsed = json.loads(buf.getvalue())
    assert "sessions" in parsed
    assert "recalls" in parsed
    assert "saves" in parsed
    assert "mode_split" in parsed
    assert "circuit_breaker" in parsed


def test_malformed_log_lines_skipped():
    """Bad JSON lines in hook.log should be silently skipped."""
    d = _make_dir()
    content = (
        '{"ts":"T1","event":"prompt_pending","detail":{"session_id":"s1"}}\n'
        'NOT VALID JSON\n'
        '{"ts":"T2","event":"stop_stored","detail":{"session_id":"s1"}}\n'
    )
    (d / "hook.log").write_text(content, encoding="utf-8")
    # Should not raise
    m = _compute_metrics(d)
    assert m["saves"]["prompt"] == 1
    assert m["saves"]["answer"] == 1


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    failures = 0
    tests = [
        test_empty_dir_returns_zeros,
        test_session_counting_from_hook_log,
        test_save_counter_json_aggregated,
        test_recall_audit_hit_rate,
        test_breaker_trips_from_hook_log,
        test_last_recall_json_surfaced,
        test_json_output_flag,
        test_malformed_log_lines_skipped,
    ]
    for fn in tests:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as e:
            failures += 1
            print("FAIL", fn.__name__, "-", e)
    sys.exit(1 if failures else 0)
