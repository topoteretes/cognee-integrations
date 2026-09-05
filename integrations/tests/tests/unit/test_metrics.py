"""Unit tests for the cognee-plugin metrics command.

Everything is derived from mock local files in a temp dir — no network, no real
plugin state. Fixtures use the SAME schemas the plugin actually writes: hook.log
lines are {ts, pid, event, detail}; mode_decision.detail.mode is "local_sdk" or
"http"; warmup-buffered saves log "store_buffered_warming"; recall-audit.log /
save_counter.json / last_recall.json match their writers.

Migrated from {claude-code,codex}/tests/test_metrics.py.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib

import pytest


@pytest.fixture
def cognee_plugin(suite, isolated_modules):
    return isolated_modules(suite, "cognee_plugin")


@pytest.fixture
def state_dir(cognee_plugin, tmp_path, monkeypatch):
    """A temp plugin-state dir that cognee_plugin reads its files from."""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(cognee_plugin, "_PLUGIN_DIR", d)
    return d


def _hook(event: str, **detail) -> dict:
    """A hook.log line as hook_log() writes it (session id, if any, in detail)."""
    return {"ts": "2026-01-01T00:00:00+00:00", "pid": 1, "event": event, "detail": detail}


def _write_jsonl(path: pathlib.Path, *entries: dict) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _run_cli(cognee_plugin, argv: list):
    """Drive main() against the patched state dir; return (rc, stdout)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cognee_plugin.main(argv)
    return rc, out.getvalue()


def test_empty_dir_returns_zeros(cognee_plugin, state_dir):
    assert cognee_plugin._compute_metrics(state_dir) == {
        "sessions": 0,
        "recalls": {"total": 0, "hits": 0, "hit_rate_pct": 0.0},
        "saves": {"prompt": 0, "trace": 0, "answer": 0},
        "mode_split": {"local_pct": 0.0, "cloud_pct": 0.0, "local_count": 0, "cloud_count": 0},
        "breaker_open_events": 0,
    }


def test_mode_split_counts_local_sdk_as_local(cognee_plugin, state_dir):
    # Regression: resolve_runtime_mode() emits "local_sdk", never "local".
    _write_jsonl(
        state_dir / "hook.log",
        _hook("mode_decision", hook="store-user-prompt", mode="local_sdk"),
        _hook("mode_decision", hook="store-to-session:tool", mode="local_sdk"),
        _hook("mode_decision", hook="store-to-session:stop", mode="http"),
    )
    ms = cognee_plugin._compute_metrics(state_dir)["mode_split"]
    assert ms["local_count"] == 2
    assert ms["cloud_count"] == 1
    assert ms["local_pct"] == round(100.0 * 2 / 3, 1)
    assert ms["cloud_pct"] == round(100.0 * 1 / 3, 1)


def test_saves_counted_once_from_hook_log(cognee_plugin, state_dir):
    # Regression: hook.log is the durable source; save_counter.json (a drain
    # buffer holding the same events) must NOT be added on top.
    _write_jsonl(
        state_dir / "hook.log",
        _hook("prompt_pending", chars=10, turn_id="t1"),
        _hook("prompt_pending", chars=12, turn_id="t2"),
        _hook("trace_stored", tool="Bash", status="ok"),
        _hook("stop_stored", chars=42),
    )
    (state_dir / "save_counter.json").write_text(
        json.dumps({"claude_s1": {"prompt": 1, "trace": 1, "answer": 1}}), encoding="utf-8"
    )
    saves = cognee_plugin._compute_metrics(state_dir)["saves"]
    assert saves == {"prompt": 2, "trace": 1, "answer": 1}


def test_saves_include_warmup_buffered(cognee_plugin, state_dir):
    # Warmup-buffered trace/answer saves log store_buffered_warming, not
    # trace_stored/stop_stored, and must still be counted.
    _write_jsonl(
        state_dir / "hook.log",
        _hook("trace_stored", tool="Bash", status="ok"),
        _hook("store_buffered_warming", hook="tool", tool="Read"),
        _hook("store_buffered_warming", hook="stop"),
        _hook("stop_stored", chars=5),
    )
    saves = cognee_plugin._compute_metrics(state_dir)["saves"]
    assert saves == {"prompt": 0, "trace": 2, "answer": 2}


def test_sessions_union_across_files(cognee_plugin, state_dir):
    _write_jsonl(
        state_dir / "hook.log",
        _hook("bootstrap_spawned", session_id="s_hook"),
        _hook("idle_watcher_restarted", session="s_hook2", dataset="x"),
        _hook("prompt_pending", chars=1, turn_id="t"),  # mainline events carry no id
    )
    (state_dir / "save_counter.json").write_text(
        json.dumps({"s_counter": {"prompt": 1, "trace": 0, "answer": 0}}), encoding="utf-8"
    )
    (state_dir / "last_recall.json").write_text(
        json.dumps({"session_id": "s_last", "ts": "T", "hits": {}}), encoding="utf-8"
    )
    _write_jsonl(
        state_dir / "recall-audit.log",
        {"ts": "T", "session_id": "s_audit", "prompt": "q", "hits": {"session": 0}},
    )
    assert cognee_plugin._compute_metrics(state_dir)["sessions"] == 5


def test_recall_total_and_hit_rate(cognee_plugin, state_dir):
    _write_jsonl(
        state_dir / "recall-audit.log",
        {"ts": "T1", "session_id": "s", "hits": {"session": 2, "graph_context": 0}},
        {"ts": "T2", "session_id": "s", "hits": {"session": 0, "graph_context": 0}},
        {"ts": "T3", "session_id": "s", "hits": {"session": 0, "graph_context": 1}},
    )
    r = cognee_plugin._compute_metrics(state_dir)["recalls"]
    assert r["total"] == 3
    assert r["hits"] == 2
    assert r["hit_rate_pct"] == round(100.0 * 2 / 3, 1)


def test_breaker_open_events_counted(cognee_plugin, state_dir):
    _write_jsonl(
        state_dir / "hook.log",
        _hook("recall_breaker_open", retry_in=90),
        _hook("recall_breaker_open", retry_in=45),
        _hook("mode_decision", hook="x", mode="http"),
    )
    assert cognee_plugin._compute_metrics(state_dir)["breaker_open_events"] == 2


def test_malformed_lines_skipped(cognee_plugin, state_dir):
    (state_dir / "hook.log").write_text(
        json.dumps(_hook("prompt_pending", chars=1))
        + "\n"
        + "NOT VALID JSON\n"
        + "\n"
        + json.dumps(_hook("stop_stored", chars=2))
        + "\n",
        encoding="utf-8",
    )
    saves = cognee_plugin._compute_metrics(state_dir)["saves"]
    assert saves["prompt"] == 1
    assert saves["answer"] == 1


def test_cli_json_output(cognee_plugin, state_dir):
    _write_jsonl(state_dir / "hook.log", _hook("mode_decision", hook="x", mode="local_sdk"))
    rc, out = _run_cli(cognee_plugin, ["metrics", "--json"])
    assert rc == 0
    parsed = json.loads(out)
    assert set(parsed) == {"sessions", "recalls", "saves", "mode_split", "breaker_open_events"}
    assert parsed["mode_split"]["local_count"] == 1


def test_cli_rollup_is_ascii(cognee_plugin, state_dir):
    rc, out = _run_cli(cognee_plugin, ["metrics"])
    assert rc == 0
    assert "Sessions" in out
    out.encode("ascii")  # raises if any non-ASCII slipped into the rollup


def test_no_command_returns_1(cognee_plugin, state_dir):
    rc, _ = _run_cli(cognee_plugin, [])
    assert rc == 1
