"""Native Codex adapters around the shared cloud memory pipeline."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[3] / "codex/plugins/cognee"


@pytest.fixture
def codex(suite):
    if suite.name != "codex":
        pytest.skip("Codex host adapter")
    return suite


@pytest.fixture
def report():
    path = PACKAGE / "skills/cognee-report/scripts/plugin_eval.py"
    spec = importlib.util.spec_from_file_location("codex_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_usage_is_delta_based_and_excludes_injected_context(report, tmp_path):
    prior = {}

    def token(total, cached, out):
        return {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": total,
                        "cached_input_tokens": cached,
                        "output_tokens": out,
                    }
                },
            },
        }

    first = report._codex_record(token(100, 20, 10), prior)
    assert first["message"]["usage"]["input_tokens"] == 80
    assert report._codex_record(token(100, 20, 10), prior) == {}
    second = report._codex_record(token(150, 40, 20), prior)
    assert second["message"]["usage"] == {
        "input_tokens": 30,
        "cache_read_input_tokens": 20,
        "output_tokens": 10,
    }
    assert (
        report._codex_record(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "injected memory"}],
                },
            },
            prior,
        )
        == {}
    )
    assert report.find_transcript(tmp_path, "../../escape") is None
    assert report.build_parser().parse_args(["report"]).agent == "codex"


def test_native_rollout_report_counts_real_usage(report, tmp_path):
    path = tmp_path / "rollout.jsonl"
    rows = [
        {
            "timestamp": "2026-10-14T10:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Actual answer"}],
            },
        },
        {
            "timestamp": "2026-10-14T10:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 200,
                        "cached_input_tokens": 50,
                        "output_tokens": 20,
                    }
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows + rows[-1:]))
    result = report.analyze_transcript(path, None, None, [])
    assert result["usage"]["input_tokens"] == 150
    assert result["usage"]["cache_read_input_tokens"] == 50
    assert result["usage"]["output_tokens"] == 20
    assert result["api_messages"] == 1
    assert result["hook_runs"] == []


def test_status_reads_only_own_counters(codex, statusline, temp_home):
    root = temp_home / ".cognee-plugin/codex"
    p = root / "sessions/one/stats.json"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps(
            {
                "entries_injected": 12,
                "recalls": 4,
                "hits": 3,
                "dedup_measurements": 2,
                "dedup_chars_saved": 800,
            }
        )
    )
    p = root / "recall/one.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"hits": {"graph_context": 1}}))
    line = statusline.render_status_for_host("one")
    assert all(
        value in line for value in ("12 memories", "1 spark", "3/4 pulses", "~200 tokens saved")
    )
    other = statusline.render_status_for_host("two")
    assert "12 memories" not in other and "tokens saved n/a" in other


@pytest.mark.parametrize("tool", ["exec_command", "functions.exec_command", "shell_command"])
@pytest.mark.parametrize(
    "result",
    [{"output": "new regression found", "exit_code": 0}, {"output": "test failed", "exit_code": 1}],
)
def test_native_shell_evidence_is_queued(
    codex, hook_module, isolated_modules, monkeypatch, tool, result
):
    store = hook_module(codex, "store-to-session.py")
    import sys

    worker = sys.modules["_cloud_worker"]
    monkeypatch.setattr(store, "cloud_enabled", lambda *a: True)
    monkeypatch.setattr(store, "_load_session", lambda: ("sid", "dataset", ""))
    monkeypatch.setattr(store, "resolve_runtime_mode", lambda: {"mode": "http"})
    events = []
    monkeypatch.setattr(worker, "enqueue", lambda *args: events.append(args))
    asyncio.run(
        store._store_tool_call(
            {"tool_name": tool, "tool_input": {"cmd": "git diff"}, "tool_output": result}
        )
    )
    assert len(events) == 1
    assert events[0][2]["origin_function"] == tool
    assert result["output"] in str(events[0][2])


def test_report_without_install_marker(report, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    info = report.load_install_info(tmp_path)
    assert info["plugin"] == "cognee" and info["version"] == "1.8.2"
    assert info["last_updated"] is None


def test_cloud_hooks_queue_native_events_without_network(codex, run_hook, payloads, temp_home):
    env = {
        "COGNEE_CODEX_BACKEND": "cloud",
        "COGNEE_IDLE_DISABLED": "true",
        "COGNEE_PLUGIN_IDENTITY": "false",
    }
    url = "https://unavailable.example.invalid"
    start = run_hook(
        codex,
        "session-start.py",
        stdin=payloads.session_start(session_id="native-host"),
        service_url=url,
        env=env,
    )
    assert start.returncode == 0, start.stderr
    assert "queued" in json.loads(start.stdout)["systemMessage"]
    prompt = run_hook(
        codex,
        "store-user-prompt.py",
        stdin=payloads.user_prompt(session_id="native-host", prompt="Remember this real task"),
        service_url=url,
        env=env,
    )
    assert prompt.returncode == 0
    json.loads(prompt.stdout)
    tool = run_hook(
        codex,
        "store-to-session.py",
        stdin=payloads.post_tool_use(
            session_id="native-host",
            tool_name="exec_command",
            tool_input={"cmd": "pytest"},
            tool_output={"output": "3 passed", "exit_code": 0},
        ),
        service_url=url,
        env=env,
    )
    stop = run_hook(
        codex,
        "store-to-session.py",
        "--stop",
        stdin=payloads.stop(session_id="native-host", assistant_message="Completed the task"),
        service_url=url,
        env=env,
    )
    assert tool.returncode == stop.returncode == 0
    root = temp_home / ".cognee-plugin/codex"
    work = [json.loads(p.read_text()) for p in (root / "spool").glob("*.json")]
    assert len(work) == 2
    assert len({row["client_event_id"] for row in work}) == 2
    qa = next(row["entry"] for row in work if row["entry"]["type"] == "qa")
    assert qa["question"] == "Remember this real task" and qa["answer"] == "Completed the task"
    assert not (temp_home / ".claude").exists()
    assert not (temp_home / ".cognee-plugin/claude-code").exists()


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_failed_inspections_are_kept(codex, hook_module, tool):
    store = hook_module(codex, "store-to-session.py")
    assert not store._low_signal_trace(tool, {"error": "permission denied"}, "error")
