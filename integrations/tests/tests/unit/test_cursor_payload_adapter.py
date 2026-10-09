"""Contracts for the Cursor hook payload adapter.

``cursor_hook.py`` is the fail-open boundary between Cursor's hook contract
(https://cursor.com/docs/hooks) and the shared Cognee hook scripts. These tests
drive it with documented Cursor payloads and temporary files only; nothing here
reads a developer's real transcript or state.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

INTEGRATIONS_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_PATH = INTEGRATIONS_ROOT / "cursor" / "scripts" / "cursor_hook.py"

#: The base fields Cursor sends with every agent hook (docs: "Common schema").
COMMON = {
    "conversation_id": "conv-123",
    "generation_id": "gen-7",
    "model": "claude-opus-4-7",
    "model_id": "claude-opus-4-7",
    "hook_event_name": "",
    "cursor_version": "1.7.2",
    "workspace_roots": ["/work/project"],
    "user_email": None,
    "transcript_path": None,
}


def _payload(event: str, **fields) -> dict:
    payload = dict(COMMON)
    payload["hook_event_name"] = event
    payload.update(fields)
    return payload


def _write_transcript(path: Path, *records: dict) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _user(text: str) -> dict:
    return {"role": "user", "message": {"content": [{"type": "text", "text": text}]}}


def _assistant(*blocks: dict) -> dict:
    return {"role": "assistant", "message": {"content": list(blocks)}}


@pytest.fixture
def adapter(monkeypatch, tmp_path) -> ModuleType:
    """Load the adapter by path with HOME pointed at a temp dir."""
    if not ADAPTER_PATH.is_file():
        pytest.skip(f"Cursor adapter has not been implemented: {ADAPTER_PATH}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("CURSOR_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CURSOR_TRANSCRIPT_PATH", raising=False)
    spec = importlib.util.spec_from_file_location("cursor_cursor_hook", ADAPTER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_adapter_module_exists_at_the_cursor_plugin_path():
    assert ADAPTER_PATH.is_file(), f"missing Cursor payload adapter: {ADAPTER_PATH}"


# --------------------------------------------------------------------------- #
# normalize_payload
# --------------------------------------------------------------------------- #


def test_session_start_maps_conversation_and_workspace(adapter):
    payload = _payload(
        "sessionStart",
        session_id="conv-123",
        is_background_agent=False,
        composer_mode="agent",
    )
    normalized = adapter.normalize_payload(payload, "session-start.py")
    assert normalized["hook_event_name"] == "SessionStart"
    assert normalized["session_id"] == "conv-123"
    assert normalized["turn_id"] == "gen-7"
    assert normalized["cwd"] == "/work/project"
    assert normalized["source"] == "startup"
    assert normalized["composer_mode"] == "agent"
    assert normalized["is_background_agent"] is False
    assert normalized["host"] == "cursor"
    assert "transcript_path" not in normalized  # null in Cursor stays absent


def test_conversation_id_is_the_session_when_session_id_is_absent(adapter):
    normalized = adapter.normalize_payload(
        _payload("beforeSubmitPrompt", prompt="hi"), "session-context-lookup.py"
    )
    assert normalized["session_id"] == "conv-123"
    assert normalized["hook_event_name"] == "UserPromptSubmit"
    assert normalized["prompt"] == "hi"


def test_cwd_falls_back_to_the_cursor_project_dir_env(adapter, monkeypatch):
    monkeypatch.setenv("CURSOR_PROJECT_DIR", "/from/env")
    payload = _payload("beforeSubmitPrompt", prompt="x", workspace_roots=[])
    assert adapter.normalize_payload(payload, "store-user-prompt.py")["cwd"] == "/from/env"


def test_explicit_cwd_beats_workspace_roots(adapter):
    payload = _payload("postToolUse", cwd="/elsewhere", tool_name="Read", tool_input={})
    assert adapter.normalize_payload(payload, "store-to-session.py")["cwd"] == "/elsewhere"


def test_post_tool_use_maps_tool_names_and_decodes_json_strings(adapter):
    payload = _payload(
        "postToolUse",
        tool_name="Shell",
        tool_input='{"command": "npm test"}',
        tool_output='{"exitCode":0,"stdout":"All tests passed"}',
        tool_use_id="abc123",
        cwd="/project",
        duration=5432,
    )
    normalized = adapter.normalize_payload(payload, "store-to-session.py")
    assert normalized["hook_event_name"] == "PostToolUse"
    assert normalized["tool_name"] == "Bash"
    assert normalized["tool_input"] == {"command": "npm test"}
    assert normalized["tool_response"] == {"exitCode": 0, "stdout": "All tests passed"}
    assert normalized["tool_call_id"] == "abc123"
    assert normalized["duration_ms"] == 5432
    assert "error" not in normalized


@pytest.mark.parametrize(
    ("cursor_name", "cognee_name"),
    [
        ("Shell", "Bash"),
        ("Task", "Agent"),
        ("Read", "Read"),
        ("Write", "Write"),
        ("Grep", "Grep"),
        ("Delete", "Delete"),
        ("MCP:cognee_recall", "mcp__cognee_recall"),
        ("SomethingNew", "SomethingNew"),
    ],
)
def test_tool_name_mapping(adapter, cursor_name, cognee_name):
    assert adapter.map_tool_name(cursor_name) == cognee_name


def test_post_tool_use_failure_records_the_error(adapter):
    payload = _payload(
        "postToolUseFailure",
        tool_name="Shell",
        tool_input={"command": "npm test"},
        tool_use_id="abc123",
        error_message="Command timed out after 30s",
        failure_type="timeout",
        duration=5000,
        is_interrupt=False,
    )
    normalized = adapter.normalize_payload(payload, "store-to-session.py")
    assert normalized["hook_event_name"] == "PostToolUse"
    assert normalized["tool_name"] == "Bash"
    assert normalized["error"] == "Command timed out after 30s"
    assert normalized["failure_type"] == "timeout"
    assert normalized["tool_response"] == ""


def test_non_object_tool_input_becomes_an_empty_dict(adapter):
    payload = _payload("postToolUse", tool_name="Read", tool_input="not json", tool_output="x")
    assert adapter.normalize_payload(payload, "store-to-session.py")["tool_input"] == {}


def test_after_agent_response_runs_the_stop_store_with_its_text(adapter):
    """The QA pair is stored from afterAgentResponse: it is the end-of-turn hook
    Cursor's IDE actually fires, and it carries the answer."""
    payload = _payload("afterAgentResponse", text="the answer")
    normalized = adapter.normalize_payload(payload, "store-to-session.py", ("--stop",))
    assert normalized["hook_event_name"] == "Stop"
    assert normalized["assistant_message"] == "the answer"
    assert normalized["last_assistant_message"] == "the answer"
    assert normalized["stop_hook_active"] is False
    assert normalized["turn_id"] == "gen-7"
    assert not adapter.should_skip(normalized, "store-to-session.py", ("--stop",))
    # Without text there is nothing to store; no transcript scraping either.
    empty = adapter.normalize_payload(
        _payload("afterAgentResponse", text=""), "store-to-session.py", ("--stop",)
    )
    assert "assistant_message" not in empty
    assert adapter.should_skip(empty, "store-to-session.py", ("--stop",))


def test_stop_skips_a_turn_after_agent_response_already_stored(adapter, tmp_path):
    transcript = _write_transcript(
        tmp_path / "t.jsonl",
        _user("question"),
        _assistant({"type": "text", "text": "from transcript"}),
    )
    assert adapter.mark_answer_stored("conv-123", "gen-7") is not None
    payload = _payload("stop", status="completed", loop_count=0, transcript_path=str(transcript))
    normalized = adapter.normalize_payload(payload, "store-to-session.py", ("--stop",))
    assert normalized["hook_event_name"] == "Stop"
    assert normalized["status"] == "completed"
    assert "assistant_message" not in normalized
    assert normalized["answer_already_stored"] is True
    assert adapter.should_skip(normalized, "store-to-session.py", ("--stop",))
    # The marker is consumed: the next stop falls back to the transcript again.
    again = adapter.normalize_payload(payload, "store-to-session.py", ("--stop",))
    assert again["assistant_message"] == "from transcript"


def test_a_stale_marker_from_another_turn_does_not_suppress_stop(adapter, tmp_path):
    transcript = _write_transcript(
        tmp_path / "t.jsonl", _user("q"), _assistant({"type": "text", "text": "answer"})
    )
    adapter.mark_answer_stored("conv-123", "gen-OLD", root=tmp_path)
    assert adapter.consume_stored_marker("conv-123", "gen-7", root=tmp_path) is False
    assert adapter.consume_stored_marker("conv-123", "gen-7", root=tmp_path) is False  # gone
    # Either side without a turn id (headless CLI) counts as the same turn.
    adapter.mark_answer_stored("conv-123", "", root=tmp_path)
    assert adapter.consume_stored_marker("conv-123", "gen-7", root=tmp_path) is True
    adapter.mark_answer_stored("conv-123", "gen-7", root=tmp_path)
    assert adapter.consume_stored_marker("conv-123", "", root=tmp_path) is True
    assert adapter.consume_stored_marker("", "gen-7", root=tmp_path) is False
    assert adapter.mark_answer_stored("", "gen-7", root=tmp_path) is None
    payload = _payload("stop", status="completed", loop_count=0, transcript_path=str(transcript))
    normalized = adapter.normalize_payload(payload, "store-to-session.py", ("--stop",))
    assert normalized["assistant_message"] == "answer"


def test_stored_marker_path_is_sanitized(adapter, tmp_path):
    path = adapter.mark_answer_stored("../evil/../id", "gen-7", root=tmp_path)
    assert path is not None
    assert path.parent == tmp_path / "responses"
    assert ".." not in path.name and "/" not in path.name
    assert path.read_text() == "gen-7"


def test_stop_falls_back_to_the_transcript_tail(adapter, tmp_path):
    transcript = _write_transcript(
        tmp_path / "t.jsonl",
        _user("first question"),
        _assistant({"type": "text", "text": "old answer"}),
        _user("second question"),
        _assistant(
            {"type": "text", "text": "Let me look."},
            {"type": "tool_use", "name": "Shell", "input": {"command": "ls"}},
        ),
        _assistant(
            {"type": "text", "text": "Final answer, "}, {"type": "text", "text": "two blocks."}
        ),
    )
    payload = _payload("stop", status="completed", loop_count=0, transcript_path=str(transcript))
    normalized = adapter.normalize_payload(payload, "store-to-session.py", ("--stop",))
    assert normalized["assistant_message"] == "Final answer, \n\ntwo blocks."


def test_stop_without_any_answer_is_skipped(adapter):
    payload = _payload("stop", status="aborted", loop_count=0)
    normalized = adapter.normalize_payload(payload, "store-to-session.py", ("--stop",))
    assert "assistant_message" not in normalized
    assert adapter.should_skip(normalized, "store-to-session.py", ("--stop",))


def test_transcript_tail_ignores_junk_and_non_regular_files(adapter, tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('not json\n{"role": "assistant"}\n[1,2]\n' + json.dumps(_user("q")) + "\n")
    records = adapter.read_transcript_tail(path)
    assert [r.get("role") for r in records] == ["assistant", "user"]
    assert adapter.read_transcript_tail(tmp_path) == []
    assert adapter.read_transcript_tail(tmp_path / "missing.jsonl") == []
    assert adapter.read_transcript_tail(None) == []
    assert adapter.last_assistant_text(records) == ""


def test_transcript_tail_drops_a_partial_first_line(adapter, tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "MAX_TRANSCRIPT_TAIL_BYTES", 120)
    first = _assistant({"type": "text", "text": "x" * 200})
    last = _assistant({"type": "text", "text": "kept"})
    _write_transcript(tmp_path / "t.jsonl", first, last)
    records = adapter.read_transcript_tail(tmp_path / "t.jsonl")
    assert [adapter.last_assistant_text([r]) for r in records] == ["kept"]


def test_pre_compact_and_session_end_carry_their_reason(adapter):
    compact = adapter.normalize_payload(
        _payload("preCompact", trigger="auto", context_usage_percent=85), "pre-compact.py"
    )
    assert compact["hook_event_name"] == "PreCompact"
    assert compact["trigger"] == "auto"
    end = adapter.normalize_payload(
        _payload("sessionEnd", reason="user_close", duration_ms=45000),
        "sync-session-to-graph.py",
        ("--session-end",),
    )
    assert end["hook_event_name"] == "SessionEnd"
    assert end["reason"] == "user_close"


def test_event_is_inferred_from_the_script_when_cursor_omits_it(adapter):
    payload = {k: v for k, v in _payload("").items() if k != "hook_event_name"}
    assert (
        adapter.normalize_payload(payload, "session-start.py")["hook_event_name"] == "SessionStart"
    )
    assert (
        adapter.normalize_payload(payload, "store-to-session.py", ("--stop",))["hook_event_name"]
        == "Stop"
    )
    assert (
        adapter.normalize_payload(payload, "store-to-session.py")["hook_event_name"]
        == "PostToolUse"
    )


def test_prompt_hooks_skip_an_empty_prompt(adapter):
    normalized = adapter.normalize_payload(
        _payload("beforeSubmitPrompt", prompt="  "), "store-user-prompt.py"
    )
    assert adapter.should_skip(normalized, "store-user-prompt.py", ())
    assert adapter.should_skip(normalized, "session-context-lookup.py", ())
    assert not adapter.should_skip({"prompt": "x"}, "session-context-lookup.py", ())


# --------------------------------------------------------------------------- #
# translate_output
# --------------------------------------------------------------------------- #


def _cognee_reply(context: str = "", message: str = "", event: str = "UserPromptSubmit") -> str:
    hso = {"hookEventName": event}
    if context:
        hso["additionalContext"] = context
    if message:
        hso["systemMessage"] = message
    return json.dumps(
        {"hookSpecificOutput": hso, **({"systemMessage": message} if message else {})}
    )


def test_prompt_context_is_returned_in_both_shapes_cursor_accepts(adapter):
    reply = adapter.translate_output(
        "UserPromptSubmit", _cognee_reply("=== Cognee memory ===\nfacts")
    )
    assert reply["continue"] is True
    assert reply["additional_context"] == "=== Cognee memory ===\nfacts"
    assert reply["hookSpecificOutput"] == {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "=== Cognee memory ===\nfacts",
    }


def test_prompt_reply_without_context_is_just_continue(adapter):
    assert adapter.translate_output("UserPromptSubmit", "{}") == {"continue": True}
    assert adapter.translate_output("UserPromptSubmit", "") == {"continue": True}
    assert adapter.translate_output("UserPromptSubmit", "not json") == {"continue": True}
    assert adapter.translate_output("UserPromptSubmit", "[1]") == {"continue": True}


def test_a_system_message_becomes_a_notice_in_context_once(adapter):
    header = "Cognee memory: 0 memory hits"
    # The recall hook repeats its header as systemMessage: no duplicate notice.
    reply = adapter.translate_output(
        "UserPromptSubmit", _cognee_reply(header + "\n\nfacts", header)
    )
    assert reply["additional_context"] == header + "\n\nfacts"
    # A distinct notice (memory off, update available) is appended for the agent to relay.
    reply = adapter.translate_output(
        "SessionStart", _cognee_reply("", "Cognee memory: hook failed")
    )
    assert reply == {"additional_context": "[cognee notice] Cognee memory: hook failed"}
    reply = adapter.translate_output("SessionStart", _cognee_reply("ctx", "note", "SessionStart"))
    assert reply["additional_context"] == "ctx\n\n[cognee notice] note"


def test_flat_system_message_from_the_hook_runner_is_relayed(adapter):
    reply = adapter.translate_output("SessionStart", json.dumps({"systemMessage": "hook x failed"}))
    assert reply == {"additional_context": "[cognee notice] hook x failed"}


def test_stop_never_forwards_anything(adapter):
    leaky = json.dumps(
        {
            "decision": "block",
            "reason": "keep going",
            "followup_message": "continue",
            "hookSpecificOutput": {"additionalContext": "x", "systemMessage": "y"},
            "systemMessage": "z",
        }
    )
    assert adapter.translate_output("Stop", leaky) == {}
    assert adapter.translate_output("SessionEnd", leaky) == {}
    assert adapter.translate_output("AfterAgentResponse", leaky) == {}


def test_post_tool_use_passes_context_and_pre_compact_a_user_message(adapter):
    assert adapter.translate_output(
        "PostToolUse", _cognee_reply("file facts", "", "PostToolUse")
    ) == {"additional_context": "file facts"}
    assert adapter.translate_output("PostToolUse", _cognee_reply("", "note", "PostToolUse")) == {}
    assert adapter.translate_output(
        "PreCompact", _cognee_reply("anchor", "compacting", "PreCompact")
    ) == {"user_message": "compacting"}
    assert adapter.translate_output("PreCompact", "{}") == {}


def test_translate_accepts_a_dict_as_well_as_text(adapter):
    reply = adapter.translate_output(
        "PostToolUse", {"hookSpecificOutput": {"additionalContext": "d"}}
    )
    assert reply == {"additional_context": "d"}


# --------------------------------------------------------------------------- #
# run_inner_hook / main
# --------------------------------------------------------------------------- #


def test_inner_hooks_launch_through_the_hook_runner_with_plugin_env(adapter):
    command = adapter.inner_command("store-to-session.py", ("--stop",))
    assert command[0] == sys.executable
    assert Path(command[1]).name == "hook_runner.py"
    assert Path(command[2]).name == "store-to-session.py"
    assert command[3:] == ["--stop"]
    env = adapter.inner_environment({"cwd": "/work/project"})
    assert env["CURSOR_CWD"] == "/work/project"
    assert Path(env["PLUGIN_ROOT"]) == ADAPTER_PATH.parent.parent
    assert Path(env["CURSOR_PLUGIN_ROOT"]) == ADAPTER_PATH.parent.parent


def test_run_inner_hook_forwards_payload_script_and_flags(adapter):
    seen = []

    def runner(payload, script, flags):
        seen.append((payload, script, flags))
        return json.dumps({"hookSpecificOutput": {"additionalContext": "ok"}})

    out = adapter.run_inner_hook({"a": 1}, "store-to-session.py", ("--stop",), runner=runner)
    assert seen == [({"a": 1}, "store-to-session.py", ("--stop",))]
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "ok"


def test_unsupported_inner_script_is_refused(adapter):
    with pytest.raises(ValueError):
        adapter._run_script({}, "rm-rf.py", ())


def test_a_slow_inner_hook_is_killed_and_the_adapter_replies_neutrally(
    adapter, monkeypatch, capsys
):
    monkeypatch.setenv("COGNEE_CURSOR_HOOK_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setattr(
        adapter,
        "inner_command",
        lambda script, flags: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    with pytest.raises(subprocess.TimeoutExpired):
        adapter._run_script({"cwd": "/tmp"}, "store-to-session.py", ())

    monkeypatch.setattr(
        "sys.stdin", _Stdin(json.dumps(_payload("postToolUse", tool_name="Read", tool_input={})))
    )
    assert adapter.main(["store-to-session.py"]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {}


class _Stdin:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text


def test_main_replies_neutrally_to_unknown_scripts_and_bad_input(adapter, capsys):
    assert adapter.main(["not-a-hook.py"]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {}
    assert adapter.main([]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {}


def test_main_skips_the_inner_hook_for_an_empty_prompt(adapter, monkeypatch, capsys):
    monkeypatch.setattr(adapter, "_run_script", lambda *a, **k: pytest.fail("must not run"))
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(_payload("beforeSubmitPrompt", prompt=""))))
    assert adapter.main(["session-context-lookup.py"]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {"continue": True}


def test_main_fails_open_on_malformed_stdin(adapter, capsys):
    if not hasattr(sys.stdin, "read"):
        pytest.skip("no stdin")
    proc = subprocess.run(
        [sys.executable, str(ADAPTER_PATH), "session-context-lookup.py"],
        input="{not json",
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(Path(os.environ["HOME"]))},
        timeout=30,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout.strip()) == {"continue": True}


def test_main_runs_the_inner_hook_and_translates_its_reply(adapter, monkeypatch, capsys):
    calls = []

    def fake_run(payload, script, flags):
        calls.append((payload["hook_event_name"], script, flags, payload["tool_name"]))
        return json.dumps({"hookSpecificOutput": {"additionalContext": "traced"}})

    monkeypatch.setattr(adapter, "_run_script", fake_run)
    monkeypatch.setattr(
        "sys.stdin",
        _Stdin(
            json.dumps(
                _payload(
                    "postToolUse", tool_name="Shell", tool_input={"command": "ls"}, tool_output="x"
                )
            )
        ),
    )
    assert adapter.main(["store-to-session.py"]) == 0
    assert calls == [("PostToolUse", "store-to-session.py", (), "Bash")]
    assert json.loads(capsys.readouterr().out.strip()) == {"additional_context": "traced"}


def test_main_records_every_invocation_in_the_adapter_log(adapter, monkeypatch, capsys):
    """adapter.log is the only record of which Cursor hooks fired; one line per
    launch with outcome ran / skipped / failed."""
    monkeypatch.setattr(
        adapter, "_run_script", lambda payload, script, flags: json.dumps({"ok": True})
    )
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(_payload("stop", status="completed"))))
    assert adapter.main(["store-to-session.py", "--stop"]) == 0  # no answer -> skipped
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(_payload("stop", status="completed"))))
    assert adapter.main(["credits-refresh.py"]) == 0  # ran
    monkeypatch.setattr("sys.stdin", _Stdin("{not json"))
    assert adapter.main(["session-context-lookup.py"]) == 0  # failed open
    capsys.readouterr()

    log = adapter.state_dir() / adapter.ADAPTER_LOG_NAME
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [r["outcome"] for r in records] == ["skipped", "ran", "failed"]
    assert records[0]["cursor_event"] == "stop" and records[0]["inner_event"] == "Stop"
    assert records[0]["session"] == "conv-123" and records[0]["turn"] == "gen-7"
    assert records[0]["flags"] == ["--stop"]
    assert records[1]["script"] == "credits-refresh.py" and records[1]["reply_keys"] == []
    assert "JSONDecodeError" in records[2]["error"]
    assert all(isinstance(r["ms"], int) and "ts" in r and "pid" in r for r in records)


def test_main_stores_the_answer_once_per_turn_across_after_response_and_stop(
    adapter, monkeypatch, capsys, tmp_path
):
    """afterAgentResponse stores the QA pair and marks the turn; the same turn's
    stop is skipped; a stop for a turn nobody stored falls back to the transcript."""
    calls: list[tuple[str, str]] = []

    def runner(payload, script, flags):
        calls.append((payload["hook_event_name"], payload.get("assistant_message", "")))
        return json.dumps({})

    monkeypatch.setattr(adapter, "_run_script", runner)
    transcript = _write_transcript(
        tmp_path / "t.jsonl", _user("q"), _assistant({"type": "text", "text": "scraped"})
    )

    after = _payload("afterAgentResponse", text="the answer", transcript_path=str(transcript))
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(after)))
    assert adapter.main(["store-to-session.py", "--stop"]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {}  # nothing leaks to Cursor

    stop = _payload("stop", status="completed", loop_count=0, transcript_path=str(transcript))
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(stop)))
    assert adapter.main(["store-to-session.py", "--stop"]) == 0
    capsys.readouterr()

    later = dict(stop, generation_id="gen-8")
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(later)))
    assert adapter.main(["store-to-session.py", "--stop"]) == 0
    capsys.readouterr()

    assert calls == [("Stop", "the answer"), ("Stop", "scraped")]
    log = adapter.state_dir() / adapter.ADAPTER_LOG_NAME
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(r["cursor_event"], r["outcome"]) for r in records] == [
        ("afterAgentResponse", "ran"),
        ("stop", "skipped"),
        ("stop", "ran"),
    ]
    assert records[1]["skip_reason"] == "answer_already_stored"
    assert not list((adapter.state_dir() / "responses").glob("*.stored"))


def test_hook_table_only_names_known_scripts_with_bounded_timeouts(adapter):
    for event, entries in adapter.HOOK_TABLE.items():
        assert event in adapter.EVENT_MAP, event
        for script, flags, timeout in entries:
            assert script in adapter.EVENT_FOR_SCRIPT, script
            assert all(flag.startswith("--") for flag in flags)
            assert adapter.SCRIPT_TIMEOUT_SECONDS[script] < timeout, (
                f"{script}: the adapter must time out before Cursor does"
            )
    assert adapter.HOOK_TABLE["afterAgentResponse"] == [("store-to-session.py", ("--stop",), 120)]
    assert ("store-to-session.py", ("--stop",), 120) in adapter.HOOK_TABLE["stop"]
