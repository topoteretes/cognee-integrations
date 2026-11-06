"""Shared fixtures and builders for the coding memory tests (SDK-1336).

Coding memory ships for Claude Code and Codex. ``_coding_memory.py``,
``_project_memory.py`` and ``send-unit.py`` are byte-identical between the two
(``test_capture_policy.py::test_coding_memory_modules_are_identical_in_claude_code_and_codex``),
so a test that exercises only those modules runs on Claude Code alone: the
``cm`` fixture skips Codex. A test whose path differs by host (``agent()``
dispatch, the cloud spool, the hook manifests, ``session-start.py``) uses
``host_cm`` or ``require_coding`` and runs on both.

The fixtures (``cm``, ``host_cm``, ``sent``, ``repo``) are registered as a
plugin by ``integrations/tests/conftest.py``; a test file that defines its own
fixture of the same name keeps it. The builders are imported by name.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

CODING_SUITES = {"claude-code", "codex"}
SESSION_KEY = "host-session-1"

#: Why a test of the identical modules skips Codex.
IDENTICAL_ON_CODEX = (
    "_coding_memory/_project_memory/send-unit are identical in Codex "
    "(test_capture_policy.py::test_coding_memory_modules_are_identical_in_claude_code_and_codex)"
)


def require_coding(suite, *, identical_modules: bool = False) -> None:
    """Skip hosts without coding memory; with ``identical_modules``, Codex too."""
    if suite.name not in CODING_SUITES:
        pytest.skip("coding memory ships for Claude Code and Codex only")
    if identical_modules and suite.name != "claude-code":
        pytest.skip(IDENTICAL_ON_CODEX)


def _load_coding_memory(suite, isolated_modules, monkeypatch):
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    module = isolated_modules(suite, "_coding_memory")
    common = isolated_modules(suite, "_plugin_common")
    common.set_session_key(SESSION_KEY)
    monkeypatch.setattr(module, "_session", lambda: ("claude_s1", "agent_sessions"))
    monkeypatch.setattr(module, "server_supported", lambda: True)
    return module


@pytest.fixture
def cm(suite, isolated_modules, monkeypatch):
    """``_coding_memory`` in coding mode, on Claude Code only (see module doc)."""
    require_coding(suite, identical_modules=True)
    return _load_coding_memory(suite, isolated_modules, monkeypatch)


@pytest.fixture
def host_cm(suite, isolated_modules, monkeypatch):
    """``_coding_memory`` in coding mode on both hosts, for host-dependent paths."""
    require_coding(suite)
    return _load_coding_memory(suite, isolated_modules, monkeypatch)


def record_sent(module, monkeypatch) -> list:
    """Entries handed to the send path, in order (no network, no buffer)."""
    entries: list = []
    monkeypatch.setattr(module, "_deliver", lambda dataset, sid, batch: entries.extend(batch))
    monkeypatch.setattr(module, "_drain", lambda *a: None)
    monkeypatch.setattr(module, "_maybe_cooldown_improve", lambda *a: None)
    return entries


@pytest.fixture
def sent(cm, monkeypatch):
    return record_sent(cm, monkeypatch)


# -- Git ------------------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A one-commit repository: README.md, src/app.py and src/old.py."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("hello\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / "src" / "old.py").write_text("y = 2\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first")
    return root


# -- Lessons and the server spec --------------------------------------------------------------


def lesson(
    body,
    *,
    kind="other",
    key="",
    date="2026-11-01",
    session="s",
    created="",
    commit="",
    files=(),
) -> dict:
    return {
        "data_id": body[:8],
        "lesson_type": kind,
        "state_key": key,
        "session_id": session,
        "session_date": date,
        "git_commit": commit,
        "files": list(files),
        "created_at": created,
        "body": body,
    }


def entry_spec(qa=("time", "prompt_time"), trace=("time",)) -> dict:
    """An OpenAPI document whose QAEntry/TraceEntry take the given properties."""
    return {
        "openapi": "3.1.0",
        "components": {
            "schemas": {
                "QAEntry": {"properties": {name: {} for name in qa}},
                "TraceEntry": {"properties": {name: {} for name in trace}},
            }
        },
    }


# -- Claude Code transcripts ------------------------------------------------------------------


def user(uid: str, text, stamp: str, **extra) -> dict:
    return {
        "type": "user",
        "uuid": uid,
        "timestamp": stamp,
        "message": {"role": "user", "content": text},
        **extra,
    }


def said(uid: str, text: str, stamp: str) -> dict:
    return {
        "type": "assistant",
        "uuid": uid,
        "timestamp": stamp,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def tool(uid: str, tool_id: str, name: str, tool_input, output, stamp: str, *, error=False):
    """A tool_use record and its tool_result."""
    use = {
        "type": "assistant",
        "uuid": uid + "-use",
        "timestamp": stamp,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
        },
    }
    result = user(
        uid + "-result",
        [{"type": "tool_result", "tool_use_id": tool_id, "content": output, "is_error": error}],
        stamp,
    )
    return [use, result]


def write_records(path: Path, records: list) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def claude_transcript(path: Path, calls: list, *, request="Fix the parser", answer="Done.") -> Path:
    """One request, its ``(tool_id, name, input, output, is_error)`` calls, one answer."""
    records = [user("u", request, "2026-11-05T10:00:00.000Z")]
    for index, (tool_id, name, tool_input, output, is_error) in enumerate(calls):
        stamp = f"2026-11-05T10:00:{10 + 2 * index:02d}.000Z"
        records += tool(f"t{index}", tool_id, name, tool_input, output, stamp, error=is_error)
    records.append(said("a", answer, "2026-11-05T10:01:00.000Z"))
    return write_records(path, records)


# -- Codex rollouts ---------------------------------------------------------------------------


def codex_item(item: dict, stamp: str) -> dict:
    return {
        "timestamp": stamp,
        "type": "event_msg",
        "payload": {"type": "item_completed", "turn_id": "t", "item": item},
    }


def codex_prompt(text: str, stamp: str) -> dict:
    return codex_item(
        {"type": "UserMessage", "id": "u", "content": [{"type": "text", "text": text}]}, stamp
    )


def codex_answer(text: str, stamp: str) -> list:
    return [
        codex_item(
            {"type": "AgentMessage", "id": "m", "content": [{"type": "Text", "text": text}]},
            stamp,
        ),
        {
            "timestamp": stamp,
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": text},
        },
    ]
