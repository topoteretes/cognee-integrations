"""A ``<private>`` span and a recognised secret never leave the hooks (SDK-1100).

One turn runs through the real UserPromptSubmit, PostToolUse and Stop scripts
against the mock server. Neither the span's content nor the secret may show up
in any plugin file (pending/, bridge/, spool/, hook.log, recall-audit.log, …)
or in any request body, while the public text around them still arrives.
"""

from __future__ import annotations

import json

import pytest

SPAN_SECRET = "SENTINEL-9f3a"
CREDENTIAL = "hunter2"
PROMPT = (
    f"Deploy notes <private>the vault code is {SPAN_SECRET}</private> and password={CREDENTIAL}"
)


def _plugin_text(home) -> str:
    root = home / ".cognee-plugin"
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


@pytest.mark.parametrize("route", ["direct", "cloud-spool", "offline-buffer"])
def test_private_span_and_secret_never_leave_the_hooks(
    suite, run_hook, payloads, mock_server, temp_home, closed_port_url, route
):
    """The same turn through each write path: sent at once, spooled, or buffered."""
    if route == "cloud-spool" and suite.name != "claude-code":
        pytest.skip("only claude-code has the cloud spool worker")
    mock_server.set_recall_results([{"question": "Deploy?", "answer": "Use the runbook."}])
    # Offline: the session starts against the server, which then goes away.
    url = closed_port_url if route == "offline-buffer" else mock_server.url
    env = {"COGNEE_BACKEND": "cloud"} if route == "cloud-spool" else None

    def run(script, *args, stdin, service_url=None):
        result = run_hook(
            suite, script, *args, stdin=stdin, service_url=service_url or url, env=env
        )
        assert result.returncode == 0, result.stderr
        return result

    run("session-start.py", stdin=payloads.session_start(), service_url=mock_server.url)
    run("session-context-lookup.py", stdin=payloads.user_prompt(prompt=PROMPT))
    run("store-user-prompt.py", stdin=payloads.user_prompt(prompt=PROMPT))
    run(
        "store-to-session.py",
        stdin=payloads.post_tool_use(
            tool_input={"command": f"echo '<private>{SPAN_SECRET}</private>'"},
            tool_response=f"password={CREDENTIAL}",
            tool_output=f"password={CREDENTIAL}",
        ),
    )
    run(
        "store-to-session.py",
        "--stop",
        stdin=payloads.stop(assistant_message=f"Done. <private>{SPAN_SECRET}</private> stays out."),
    )

    sent = json.dumps(mock_server.calls)
    on_disk = _plugin_text(temp_home)
    for secret in (SPAN_SECRET, CREDENTIAL):
        assert secret not in sent
        assert secret not in on_disk

    # Not vacuous: the turn really took this route, public text intact.
    state = temp_home / ".cognee-plugin"
    if route == "direct":
        queries = [
            call["json"].get("query", "")
            for call in mock_server.calls
            if call["path"] == "/api/v1/recall" and isinstance(call.get("json"), dict)
        ]
        assert any("Deploy notes [private]" in query for query in queries), queries
        stored = [
            json.dumps(call["json"])
            for call in mock_server.calls
            if call["path"] == "/api/v1/remember/entry"
        ]
        assert any("Deploy notes [private]" in body for body in stored), stored
        assert list(state.rglob("recall-audit.log"))
    else:
        buffer = "spool" if route == "cloud-spool" else "bridge"
        held = [p for p in state.rglob("*") if p.is_file() and buffer in p.parts]
        held_text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in held)
        sent_text = sent if route == "cloud-spool" else ""
        assert "Deploy notes [private]" in held_text + sent_text, [str(p) for p in held]


def test_antigravity_native_turn_keeps_the_span_out(run_hook, mock_server, tmp_path, temp_home):
    """Antigravity's adapter reads the turn from its own transcript, not the hook payload."""
    from utils.suites import ANTIGRAVITY

    transcript = tmp_path / "transcript.jsonl"
    records = []
    common = {
        "conversationId": "native-private",
        "workspacePaths": [str(tmp_path)],
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

    mock_server.set_recall_results([{"question": "Deploy?", "answer": "Use the runbook."}])
    record(source="USER_EXPLICIT", type="USER_INPUT", content=PROMPT)
    run("session-context-lookup.py", invocationNum=0)
    run("store-user-prompt.py", invocationNum=0)
    tool = {"id": "call-0", "name": "run_command", "args": {"CommandLine": f"echo {CREDENTIAL}"}}
    record(source="MODEL", tool_calls=[tool])
    record(
        source="MODEL", type="RUN_COMMAND", tool_call_id="call-0", content=f"password={CREDENTIAL}"
    )
    run("store-to-session.py", toolCall=tool, stepIdx=len(records) - 1)
    record(source="MODEL", type="PLANNER_RESPONSE", content=f"<private>{SPAN_SECRET}</private> ok")
    run("store-to-session.py", "--stop", executionNum=0, fullyIdle=True)

    sent = json.dumps(mock_server.calls)
    on_disk = _plugin_text(temp_home)
    assert SPAN_SECRET not in sent and SPAN_SECRET not in on_disk
    assert f"password={CREDENTIAL}" not in sent and f"password={CREDENTIAL}" not in on_disk
    qa = [
        call["json"]["entry"]
        for call in mock_server.calls
        if call["path"] == "/api/v1/remember/entry" and call["json"]["entry"]["type"] == "qa"
    ]
    assert qa and qa[0]["question"].startswith("Deploy notes [private]"), qa
