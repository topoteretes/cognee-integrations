from cognee_integration_tapes_cassette.transcript import (
    build_transcript,
    get_status,
    summarize_tool_input,
)

from .conftest import SESSION_COMPLETED


def test_build_transcript_contains_header_and_turns():
    text = build_transcript(SESSION_COMPLETED)
    assert "Session ID: 11111111-1111-1111-1111-111111111111" in text
    assert "Harness: claude-code" in text
    assert "User: Why does login fail for SSO users?" in text
    assert "The SSO callback drops the session cookie." in text


def test_build_transcript_orders_spans_and_summarizes_tools():
    text = build_transcript(SESSION_COMPLETED)
    tool_line = "[used tool: Bash(command: grep -r set_cookie auth/)]"
    assert tool_line in text
    # seq=1 (tool use) must come before seq=2 (text) despite input order.
    assert text.index(tool_line) < text.index("The SSO callback")


def test_build_transcript_skips_thinking_and_non_main_spans():
    text = build_transcript(SESSION_COMPLETED)
    assert "secret reasoning" not in text
    assert "SUBAGENT NOISE" not in text


def test_build_transcript_empty_for_no_traces():
    assert build_transcript({"session": {"id": "x"}, "traces": []}) == ""


def test_get_status_handles_missing_rollup():
    assert get_status({"session": {}}) == ""
    assert get_status(SESSION_COMPLETED) == "completed"


def test_summarize_tool_input_truncates_and_ignores_non_dicts():
    assert summarize_tool_input("not a dict") == ""
    assert summarize_tool_input({"irrelevant": 1}) == ""
    long_command = "x" * 200
    summary = summarize_tool_input({"command": long_command})
    assert summary.endswith("...)")
    assert len(summary) < 120
