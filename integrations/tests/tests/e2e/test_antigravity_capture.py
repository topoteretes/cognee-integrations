"""Native Antigravity payloads reach real hook scripts and a fake HTTP backend."""

import json

from utils.suites import ANTIGRAVITY


def test_native_turns_recall_capture_and_sync_without_duplicate_writes(
    run_hook, mock_server, tmp_path, project_dir
):
    transcript = tmp_path / "transcript.jsonl"
    records = []
    common = {
        "conversationId": "native-e2e",
        "workspacePaths": [str(project_dir)],
        "transcriptPath": str(transcript),
    }

    def record(**fields):
        records.append({"status": "DONE", "step_index": len(records), **fields})
        transcript.write_text("".join(json.dumps(row) + "\n" for row in records))

    def run(script, *args, **payload):
        result = run_hook(
            ANTIGRAVITY,
            "agy_hook.py",
            script,
            *args,
            stdin={**common, **payload},
            service_url=mock_server.url,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    mock_server.set_recall_results(
        [{"question": "How to test?", "answer": "Project uses deterministic tests."}]
    )
    for turn in range(2):
        record(source="USER_EXPLICIT", type="USER_INPUT", content=f"Please test turn {turn}")
        recalled = run("session-context-lookup.py", invocationNum=0)
        assert "Project uses deterministic tests." in recalled["injectSteps"][0]["ephemeralMessage"]
        run("store-user-prompt.py", invocationNum=0)
        run("store-user-prompt.py", invocationNum=0)
        tool = {"id": f"call-{turn}", "name": "run_command", "args": {"CommandLine": "pytest"}}
        record(source="MODEL", tool_calls=[tool])
        record(source="MODEL", type="RUN_COMMAND", tool_call_id=tool["id"], content="passed")
        for _retry in range(2):
            run("store-to-session.py", toolCall=tool, stepIdx=len(records) - 1)
        record(source="MODEL", type="PLANNER_RESPONSE", content=f"Turn {turn} passed")
        for _retry in range(2):
            run("store-to-session.py", "--stop", executionNum=0, fullyIdle=True)

    entries = [
        call["json"]["entry"]
        for call in mock_server.calls
        if call["path"] == "/api/v1/remember/entry"
    ]
    assert len(entries) == 4
    qa = [entry for entry in entries if entry["type"] == "qa"]
    assert [(entry["question"], entry["answer"]) for entry in qa] == [
        (f"Please test turn {turn}", f"Turn {turn} passed") for turn in range(2)
    ]
    result = run_hook(
        ANTIGRAVITY,
        "sync-session-to-graph.py",
        "--strict",
        stdin={"session_id": common["conversationId"]},
        service_url=mock_server.url,
    )
    assert result.returncode == 0, result.stderr
    mock_server.assert_called("POST", "/api/v1/improve")
    mock_server.assert_not_called("POST", "/api/v1/agents/unregister")
