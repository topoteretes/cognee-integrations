"""Unit tests for the cognee-plugin metrics command.

Everything is derived from mock local files in a temp dir - no network, no real
plugin state. Fixtures use the SAME schemas the plugin actually writes: hook.log
lines are {ts, pid, event, detail}; mode_decision.detail.mode is "local_sdk" or
"http"; warmup-buffered saves log "store_buffered_warming"; recall-audit.log /
save_counter.json / last_recall.json match their writers.

Run: python integrations/codex/tests/test_metrics.py  (or via pytest)
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts")
)

import cognee_plugin  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_dir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix="cognee-metrics-test-"))


def _hook(event: str, **detail) -> dict:
    """A hook.log line as hook_log() writes it (session id, if any, in detail)."""
    return {"ts": "2026-01-01T00:00:00+00:00", "pid": 1, "event": event, "detail": detail}


def _write_jsonl(path: pathlib.Path, *entries: dict) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _run_cli(plugin_dir: pathlib.Path, argv: list):
    """Drive main() against a temp state dir; return (rc, stdout)."""
    original = cognee_plugin._PLUGIN_DIR
    cognee_plugin._PLUGIN_DIR = plugin_dir
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cognee_plugin.main(argv)
    finally:
        cognee_plugin._PLUGIN_DIR = original
    return rc, out.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_dir_returns_zeros():
    assert cognee_plugin._compute_metrics(_fresh_dir()) == {
        "sessions": 0,
        "recalls": {"total": 0, "hits": 0, "hit_rate_pct": 0.0},
        "saves": {"prompt": 0, "trace": 0, "answer": 0},
        "mode_split": {"local_pct": 0.0, "cloud_pct": 0.0, "local_count": 0, "cloud_count": 0},
        "breaker_open_events": 0,
    }


def test_mode_split_counts_local_sdk_as_local():
    # Regression: resolve_runtime_mode() emits "local_sdk", never "local".
    d = _fresh_dir()
    _write_jsonl(
        d / "hook.log",
        _hook("mode_decision", hook="store-user-prompt", mode="local_sdk"),
        _hook("mode_decision", hook="store-to-session:tool", mode="local_sdk"),
        _hook("mode_decision", hook="store-to-session:stop", mode="http"),
    )
    ms = cognee_plugin._compute_metrics(d)["mode_split"]
    assert ms["local_count"] == 2
    assert ms["cloud_count"] == 1
    assert ms["local_pct"] == round(100.0 * 2 / 3, 1)
    assert ms["cloud_pct"] == round(100.0 * 1 / 3, 1)


def test_saves_counted_once_from_hook_log():
    # Regression: hook.log is the durable source; save_counter.json (a drain
    # buffer holding the same events) must NOT be added on top.
    d = _fresh_dir()
    _write_jsonl(
        d / "hook.log",
        _hook("prompt_pending", chars=10, turn_id="t1"),
        _hook("prompt_pending", chars=12, turn_id="t2"),
        _hook("trace_stored", tool="Bash", status="ok"),
        _hook("stop_stored", chars=42),
    )
    (d / "save_counter.json").write_text(
        json.dumps({"codex_s1": {"prompt": 1, "trace": 1, "answer": 1}}), encoding="utf-8"
    )
    assert cognee_plugin._compute_metrics(d)["saves"] == {"prompt": 2, "trace": 1, "answer": 1}


def test_saves_include_warmup_buffered():
    # Warmup-buffered trace/answer saves log store_buffered_warming, not
    # trace_stored/stop_stored, and must still be counted.
    d = _fresh_dir()
    _write_jsonl(
        d / "hook.log",
        _hook("trace_stored", tool="Bash", status="ok"),
        _hook("store_buffered_warming", hook="tool", tool="Read"),
        _hook("store_buffered_warming", hook="stop"),
        _hook("stop_stored", chars=5),
    )
    assert cognee_plugin._compute_metrics(d)["saves"] == {"prompt": 0, "trace": 2, "answer": 2}


def test_sessions_union_across_files():
    d = _fresh_dir()
    _write_jsonl(
        d / "hook.log",
        _hook("bootstrap_spawned", session_id="s_hook"),
        _hook("idle_watcher_restarted", session="s_hook2", dataset="x"),
        _hook("prompt_pending", chars=1, turn_id="t"),  # mainline events carry no id
    )
    (d / "save_counter.json").write_text(
        json.dumps({"s_counter": {"prompt": 1, "trace": 0, "answer": 0}}), encoding="utf-8"
    )
    (d / "last_recall.json").write_text(
        json.dumps({"session_id": "s_last", "ts": "T", "hits": {}}), encoding="utf-8"
    )
    _write_jsonl(
        d / "recall-audit.log",
        {"ts": "T", "session_id": "s_audit", "prompt": "q", "hits": {"session": 0}},
    )
    assert cognee_plugin._compute_metrics(d)["sessions"] == 5


def test_recall_total_and_hit_rate():
    d = _fresh_dir()
    _write_jsonl(
        d / "recall-audit.log",
        {"ts": "T1", "session_id": "s", "hits": {"session": 2, "graph_context": 0}},
        {"ts": "T2", "session_id": "s", "hits": {"session": 0, "graph_context": 0}},
        {"ts": "T3", "session_id": "s", "hits": {"session": 0, "graph_context": 1}},
    )
    r = cognee_plugin._compute_metrics(d)["recalls"]
    assert r["total"] == 3
    assert r["hits"] == 2
    assert r["hit_rate_pct"] == round(100.0 * 2 / 3, 1)


def test_breaker_open_events_counted():
    d = _fresh_dir()
    _write_jsonl(
        d / "hook.log",
        _hook("recall_breaker_open", retry_in=90),
        _hook("recall_breaker_open", retry_in=45),
        _hook("mode_decision", hook="x", mode="http"),
    )
    assert cognee_plugin._compute_metrics(d)["breaker_open_events"] == 2


def test_malformed_lines_skipped():
    d = _fresh_dir()
    (d / "hook.log").write_text(
        json.dumps(_hook("prompt_pending", chars=1))
        + "\n"
        + "NOT VALID JSON\n"
        + "\n"
        + json.dumps(_hook("stop_stored", chars=2))
        + "\n",
        encoding="utf-8",
    )
    saves = cognee_plugin._compute_metrics(d)["saves"]
    assert saves["prompt"] == 1
    assert saves["answer"] == 1


def test_cli_json_output():
    d = _fresh_dir()
    _write_jsonl(d / "hook.log", _hook("mode_decision", hook="x", mode="local_sdk"))
    rc, out = _run_cli(d, ["metrics", "--json"])
    assert rc == 0
    parsed = json.loads(out)
    assert set(parsed) == {"sessions", "recalls", "saves", "mode_split", "breaker_open_events"}
    assert parsed["mode_split"]["local_count"] == 1


def test_cli_rollup_is_ascii():
    rc, out = _run_cli(_fresh_dir(), ["metrics"])
    assert rc == 0
    assert "Sessions" in out
    out.encode("ascii")  # raises if any non-ASCII slipped into the rollup


def test_no_command_returns_1():
    rc, _ = _run_cli(_fresh_dir(), [])
    assert rc == 1


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, "-", exc)
    sys.exit(1 if failures else 0)
