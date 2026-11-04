import asyncio

import pytest


@pytest.fixture
def store(suite, hook_module, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("Claude trace filtering")
    module = hook_module(suite, "store-to-session.py")
    monkeypatch.setattr(module, "cloud_enabled", lambda *args: True)
    monkeypatch.setenv("COGNEE_SESSION_KEY", "quality-host")
    return module


@pytest.mark.parametrize(
    "tool,output,status,skip",
    [
        ("Read", "lots of file contents", "success", True),
        ("Grep", "matches", "success", True),
        ("Glob", ["a.py"], "success", True),
        ("Bash", {"stdout": "", "stderr": "", "exit_code": 0}, "success", True),
        # The shape Claude Code actually sends for a command with no output.
        (
            "Bash",
            {
                "stdout": "",
                "stderr": "",
                "interrupted": False,
                "isImage": False,
                "noOutputExpected": False,
            },
            "success",
            True,
        ),
        ("Bash", {"stdout": "built", "stderr": "", "isImage": False}, "success", False),
        ("Bash", {"stdout": "", "stderr": "", "interrupted": True}, "success", False),
        ("Bash", {"stdout": "", "stderr": "", "isImage": True}, "success", False),
        ("Bash", "", "success", True),
        ("Bash", "changed 3 files", "success", False),
        ("Bash", "", "error", False),
        ("Bash", {"exit_code": 1}, "success", False),
        ("Bash", {"unknown": ""}, "success", False),
        ("Edit", "", "success", False),
    ],
)
def test_low_signal_filter_preserves_failures_and_changes(store, tool, output, status, skip):
    assert store._low_signal_trace(tool, output, status) is skip


def test_skipped_trace_increments_stats_without_upload(store, temp_home):
    asyncio.run(
        store._store_tool_call(
            {"tool_name": "Read", "tool_input": {"file_path": "app.py"}, "tool_response": "code"}
        )
    )
    import json

    path = temp_home / ".cognee-plugin/claude-code/sessions/quality-host/stats.json"
    assert json.loads(path.read_text())["traces_skipped"] == 1
