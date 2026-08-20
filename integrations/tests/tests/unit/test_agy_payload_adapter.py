"""Contracts for the Antigravity hook payload adapter.

Antigravity hook stdin deliberately carries less context than the Cognee hook
scripts consume.  ``agy_hook.py`` is the small, fail-open boundary that fills
that gap from an explicitly supplied transcript path; these tests use only
temporary JSONL files so they can never inspect a developer's real transcript.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from types import ModuleType

import pytest

INTEGRATIONS_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_PATH = INTEGRATIONS_ROOT / "antigravity" / "scripts" / "agy_hook.py"


def _write_transcript(path: Path, *records: dict) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


@pytest.fixture
def adapter_loader():
    """Load the adapter by path, exactly as a standalone hook is installed."""
    if not ADAPTER_PATH.is_file():
        pytest.skip(f"Antigravity adapter has not been implemented: {ADAPTER_PATH}")

    loaded_names: list[str] = []

    def _load() -> ModuleType:
        spec = importlib.util.spec_from_file_location("antigravity_agy_hook", ADAPTER_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        loaded_names.append(spec.name)
        spec.loader.exec_module(module)
        return module

    try:
        yield _load
    finally:
        for name in loaded_names:
            sys.modules.pop(name, None)


@pytest.fixture
def adapter(adapter_loader) -> ModuleType:
    return adapter_loader()


def test_adapter_module_exists_at_the_antigravity_plugin_path():
    assert ADAPTER_PATH.is_file(), f"missing Antigravity payload adapter: {ADAPTER_PATH}"


def test_normalize_payload_maps_antigravity_common_fields(adapter, tmp_path):
    transcript = _write_transcript(tmp_path / "turn.jsonl")
    normalized = adapter.normalize_payload(
        {
            "conversationId": "conversation-17",
            "workspacePaths": ["/work/repo", "/work/ignored"],
            "transcriptPath": str(transcript),
            "modelName": "gemini-3-pro",
        },
        "session-start.py",
    )

    assert normalized["session_id"] == "conversation-17"
    assert normalized["cwd"] == "/work/repo"
    assert normalized["transcript_path"] == str(transcript)
    assert normalized["model"] == "gemini-3-pro"


def test_normalize_payload_keeps_explicit_snake_case_fields(adapter, tmp_path):
    transcript = _write_transcript(tmp_path / "camel.jsonl")
    normalized = adapter.normalize_payload(
        {
            "conversationId": "camel-session",
            "session_id": "canonical-session",
            "workspacePaths": ["/camel/cwd"],
            "cwd": "/canonical/cwd",
            "transcriptPath": str(transcript),
            "transcript_path": "/canonical/transcript.jsonl",
            "modelName": "camel-model",
            "model": "canonical-model",
        },
        "session-start.py",
    )

    assert normalized["session_id"] == "canonical-session"
    assert normalized["cwd"] == "/canonical/cwd"
    assert normalized["transcript_path"] == "/canonical/transcript.jsonl"
    assert normalized["model"] == "canonical-model"


@pytest.mark.parametrize(
    ("script", "stop", "event"),
    [
        ("session-start.py", False, "SessionStart"),
        ("session-context-lookup.py", False, "UserPromptSubmit"),
        ("store-user-prompt.py", False, "UserPromptSubmit"),
        ("store-to-session.py", False, "PostToolUse"),
        ("store-to-session.py", True, "Stop"),
        ("sync-session-to-graph.py", False, "SessionEnd"),
    ],
)
def test_normalize_payload_maps_target_script_to_cognee_event(adapter, script, stop, event):
    normalized = adapter.normalize_payload({"conversationId": "c-event"}, script, stop=stop)

    assert normalized["hook_event_name"] == event


@pytest.mark.parametrize("script", ["session-context-lookup.py", "store-user-prompt.py"])
def test_prompt_hooks_take_latest_explicit_user_record_from_transcript(adapter, tmp_path, script):
    transcript = _write_transcript(
        tmp_path / "prompt.jsonl",
        {
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": "first question",
            "step_index": 2,
        },
        {
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": "latest question",
            "step_index": 4,
        },
    )

    normalized = adapter.normalize_payload(
        {"conversationId": "c-1", "transcriptPath": str(transcript)}, script
    )

    assert normalized["prompt"] == "latest question"
    assert normalized["turn_id"] == "4"


def test_post_tool_use_selects_result_and_nearest_matching_model_tool_call(adapter, tmp_path):
    transcript = _write_transcript(
        tmp_path / "tool.jsonl",
        {
            "source": "MODEL",
            "status": "DONE",
            "step_index": 20,
            "tool_calls": [{"id": "old", "name": "ignored", "args": {}}],
        },
        {
            "source": "MODEL",
            "status": "DONE",
            "step_index": 21,
            "tool_calls": [{"id": "call-17", "name": "shell", "args": {"command": "git status"}}],
        },
        {
            "source": "MODEL",
            "type": "RUN_COMMAND",
            "status": "DONE",
            "step_index": 22,
            "tool_call_id": "call-17",
            "content": "first command result",
            "exit_code": 0,
        },
        {
            "source": "MODEL",
            "type": "RUN_COMMAND",
            "status": "DONE",
            "step_index": 23,
            "tool_call_id": "call-17",
            "content": "fatal: not a git repository",
            "exit_code": 128,
            "error": "command failed",
        },
    )

    normalized = adapter.normalize_payload(
        {
            "conversationId": "c-tools",
            "transcriptPath": str(transcript),
            "stepIdx": 23,
        },
        "store-to-session.py",
    )

    assert normalized["hook_event_name"] == "PostToolUse"
    assert normalized["tool_name"] == "shell"
    assert normalized["tool_input"] == {"command": "git status"}
    assert normalized["tool_response"] == "fatal: not a git repository"
    assert normalized["tool_response"] != "first command result"
    assert normalized["error"] == "command failed"
    assert normalized["exit_code"] == 128


def test_stop_uses_latest_visible_assistant_message_after_user_record(adapter, tmp_path):
    transcript = _write_transcript(
        tmp_path / "stop.jsonl",
        {
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "Visible answer from the previous turn.",
            "step_index": 1,
        },
        {
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": "new request",
            "step_index": 2,
        },
        {
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "",
            "step_index": 3,
        },
        {
            "source": "MODEL",
            "type": "THINKING",
            "status": "DONE",
            "content": "private chain of thought",
            "step_index": 4,
            "tool_calls": [{"id": "thinking-tool", "name": "shell", "args": {"command": "pwd"}}],
        },
        {
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "First visible response after the user.",
            "step_index": 5,
        },
        {
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "Later visible response after the user.",
            "step_index": 6,
        },
    )

    normalized = adapter.normalize_payload(
        {"conversationId": "c-stop", "transcriptPath": str(transcript)},
        "store-to-session.py",
        stop=True,
    )

    assert normalized["hook_event_name"] == "Stop"
    assert normalized["assistant_message"] == "Later visible response after the user."
    assert normalized["last_assistant_message"] == "Later visible response after the user."
    assert "Visible answer from the previous turn." not in str(normalized)
    assert "private chain of thought" not in str(normalized)


@pytest.mark.parametrize(
    "content",
    [
        "{not json}",
        "",
        "[]",
        json.dumps({"hookSpecificOutput": {"additionalContext": ""}}),
    ],
)
def test_translate_stdout_returns_empty_object_for_non_injectable_output(adapter, content):
    assert adapter.translate_stdout(content) == {}


def test_translate_stdout_converts_additional_context_to_antigravity_inject_step(adapter):
    translated = adapter.translate_stdout(
        json.dumps({"hookSpecificOutput": {"additionalContext": "Remember this decision."}})
    )

    assert translated == {"injectSteps": [{"ephemeralMessage": "Remember this decision."}]}


def test_translate_stdout_uses_top_level_system_message_as_additional_context_fallback(adapter):
    translated = adapter.translate_stdout(json.dumps({"systemMessage": "Fallback memory context."}))

    assert translated == {"injectSteps": [{"ephemeralMessage": "Fallback memory context."}]}


@pytest.mark.parametrize("content", ["{", '{"type": "USER_INPUT"}'])
def test_malformed_or_truncated_transcript_is_fail_open(adapter, tmp_path, content):
    transcript = tmp_path / "broken.jsonl"
    transcript.write_text(content, encoding="utf-8")

    normalized = adapter.normalize_payload(
        {"conversationId": "c-broken", "transcriptPath": str(transcript)},
        "store-user-prompt.py",
    )

    assert normalized["session_id"] == "c-broken"
    assert "prompt" not in normalized


def test_unreadable_transcript_is_fail_open(adapter, tmp_path):
    unreadable = tmp_path / "not-a-transcript"
    unreadable.mkdir()

    normalized = adapter.normalize_payload(
        {"conversationId": "c-unreadable", "transcriptPath": str(unreadable)},
        "session-context-lookup.py",
    )

    assert normalized["session_id"] == "c-unreadable"
    assert "prompt" not in normalized


def test_mixed_transcript_keeps_latest_valid_user_record_after_bad_lines(adapter, tmp_path):
    transcript = tmp_path / "mixed.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "status": "DONE",
                        "content": "older valid prompt",
                        "step_index": 4,
                    }
                ),
                '{"source":"USER_EXPLICIT"',
                "[]",
                json.dumps(
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "status": "DONE",
                        "content": "latest valid prompt",
                        "step_index": 8,
                    }
                ),
                '{"truncated":',
            ]
        ),
        encoding="utf-8",
    )

    normalized = adapter.normalize_payload(
        {"conversationId": "c-mixed", "transcriptPath": str(transcript)},
        "store-user-prompt.py",
    )

    assert normalized["prompt"] == "latest valid prompt"
    assert normalized["turn_id"] == "8"


def test_bounded_transcript_tail_discards_partial_first_line_and_finds_latest_user(
    adapter, tmp_path
):
    transcript = tmp_path / "huge.jsonl"
    latest = {
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "content": "latest prompt after giant line",
        "step_index": 99,
    }
    transcript.write_bytes(
        b'{"discarded":"' + b"x" * 1_048_700 + b'"}\n' + json.dumps(latest).encode() + b"\n"
    )

    assert transcript.stat().st_size > 1_048_576
    assert adapter.MAX_TRANSCRIPT_TAIL_BYTES <= 1_048_576
    normalized = adapter.normalize_payload(
        {"conversationId": "c-tail", "transcriptPath": str(transcript)},
        "session-context-lookup.py",
    )

    assert normalized["prompt"] == "latest prompt after giant line"
    assert normalized["turn_id"] == "99"


def test_bounded_transcript_tail_does_not_recover_early_prompt_outside_read_window(
    adapter, tmp_path
):
    transcript = tmp_path / "early-prompt.jsonl"
    early_prompt = {
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "content": "early prompt must be outside the tail",
        "step_index": 1,
    }
    irrelevant = {
        "source": "MODEL",
        "type": "THINKING",
        "status": "DONE",
        "content": "x" * 1_000,
    }
    transcript.write_text(
        json.dumps(early_prompt) + "\n" + (json.dumps(irrelevant) + "\n") * 1_100,
        encoding="utf-8",
    )

    assert transcript.stat().st_size > 1_048_576
    normalized = adapter.normalize_payload(
        {"conversationId": "c-early", "transcriptPath": str(transcript)},
        "store-user-prompt.py",
    )

    assert "prompt" not in normalized


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO support")
def test_transcript_replacement_with_a_non_writing_fifo_cannot_block_the_reader(tmp_path):
    transcript = _write_transcript(
        tmp_path / "replaceable.jsonl",
        {
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": "descriptor validation",
            "step_index": 1,
        },
    )
    child = f"""\
import importlib.util
import json
import os
from pathlib import Path

adapter_path = Path({str(ADAPTER_PATH)!r})
transcript = Path({str(transcript)!r})
spec = importlib.util.spec_from_file_location("agy_fifo_race", adapter_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original_stat = Path.stat
original_open = os.open
replaced = False

def replace_target():
    global replaced
    if not replaced:
        replaced = True
        transcript.unlink()
        os.mkfifo(transcript)

def replace_after_stat(path, *args, **kwargs):
    result = original_stat(path, *args, **kwargs)
    if path == transcript:
        replace_target()
    return result

def replace_before_open(path, flags, *args, **kwargs):
    if Path(path) == transcript:
        replace_target()
    return original_open(path, flags, *args, **kwargs)

Path.stat = replace_after_stat
os.open = replace_before_open
print(json.dumps(module.read_transcript_tail(transcript)))
"""

    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", child],
        text=True,
        capture_output=True,
        check=False,
        timeout=1.0,
    )

    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 1.0
    assert json.loads(result.stdout) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-liveness assertion")
def test_sleeping_inner_child_is_killed_by_adapter_owned_timeout_without_done_marker(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    adapter_copy = scripts / "agy_hook.py"
    shutil.copy2(ADAPTER_PATH, adapter_copy)
    child_pid_path = tmp_path / "child.pid"
    (scripts / "session-start.py").write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['CHILD_PID_PATH']).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CHILD_PID_PATH": str(child_pid_path),
        "COGNEE_AGY_HOOK_TIMEOUT_SECONDS": "0.2",
    }
    process = subprocess.Popen(
        [sys.executable, str(adapter_copy), "session-start.py"],
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    started = time.monotonic()
    timed_out = False
    child_alive_after_adapter = False
    try:
        stdout, stderr = process.communicate(
            json.dumps({"conversationId": "timeout-conversation"}), timeout=3.0
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate()
    finally:
        if child_pid_path.exists():
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                pass
            else:
                child_alive_after_adapter = True
                os.kill(child_pid, signal.SIGKILL)

    assert not timed_out, "adapter did not enforce its shorter child timeout"
    assert process.returncode == 0, stderr
    assert time.monotonic() - started < 3.0
    assert json.loads(stdout) == {}
    assert child_pid_path.is_file()
    assert not child_alive_after_adapter, "adapter returned while its child was still alive"
    assert not list((home / ".cognee-plugin").rglob("*.done"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-liveness assertion")
def test_successful_leader_with_stdio_holding_descendant_returns_without_killing_descendant(
    tmp_path,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    adapter_copy = scripts / "agy_hook.py"
    shutil.copy2(ADAPTER_PATH, adapter_copy)
    descendant_pid_path = tmp_path / "descendant.pid"
    (scripts / "session-start.py").write_text(
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "Path(os.environ['DESCENDANT_PID_PATH']).write_text(\n"
        "    str(child.pid), encoding='utf-8'\n"
        ")\n"
        "print('{}')\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "DESCENDANT_PID_PATH": str(descendant_pid_path),
        "COGNEE_AGY_HOOK_TIMEOUT_SECONDS": "0.2",
    }
    process = subprocess.Popen(
        [sys.executable, str(adapter_copy), "session-start.py"],
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    started = time.monotonic()
    adapter_timed_out = False
    descendant_alive = False
    try:
        try:
            stdout, stderr = process.communicate(
                json.dumps({"conversationId": "detached-conversation"}), timeout=3.0
            )
        except subprocess.TimeoutExpired:
            adapter_timed_out = True
            process.kill()
            stdout, stderr = process.communicate()

        assert descendant_pid_path.is_file()
        descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            descendant_alive = False
        else:
            descendant_alive = True
    finally:
        if descendant_pid_path.exists():
            descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert not adapter_timed_out, "descendant-held stdio blocked adapter completion"
    assert process.returncode == 0, stderr
    assert time.monotonic() - started < 3.0
    assert json.loads(stdout) == {}
    assert descendant_alive, "successful adapter cleanup killed an intentional detached worker"


def test_session_start_runs_once_per_conversation(adapter, tmp_path):
    payload = {"conversationId": "conversation-once"}
    calls = []

    def runner(inner_payload, script):
        calls.append((inner_payload, script))
        return {"ran": True}

    adapter.run_inner_hook(payload, "session-start.py", runner=runner, marker_dir=tmp_path)
    assert calls == [(payload, "session-start.py")]
    marker_files = list(tmp_path.rglob("*.done"))
    assert len(marker_files) == 1
    marker = marker_files[0]
    assert marker.relative_to(tmp_path).parts == (marker.name,)
    assert re.fullmatch(r"[0-9a-f]{64}\.done", marker.name)
    marker_text = marker.read_text(encoding="utf-8")
    for sensitive in ("conversation-once", "session-start.py"):
        assert sensitive not in str(marker.relative_to(tmp_path))
        assert sensitive not in marker_text
    assert (
        adapter.run_inner_hook(payload, "session-start.py", runner=runner, marker_dir=tmp_path)
        == {}
    )
    assert calls == [(payload, "session-start.py")]


@pytest.mark.parametrize("script", ["session-context-lookup.py", "store-user-prompt.py"])
def test_prompt_hooks_run_once_per_conversation_turn_and_script(adapter, tmp_path, script):
    payload = {"conversationId": "conversation-turn", "turn_id": "17"}
    calls = []

    def runner(inner_payload, inner_script):
        calls.append((inner_payload, inner_script))
        return {}

    adapter.run_inner_hook(payload, script, runner=runner, marker_dir=tmp_path)
    assert adapter.run_inner_hook(payload, script, runner=runner, marker_dir=tmp_path) == {}
    assert calls == [(payload, script)]
    adapter.run_inner_hook({**payload, "turn_id": "18"}, script, runner=runner, marker_dir=tmp_path)
    other_script = (
        "store-user-prompt.py"
        if script == "session-context-lookup.py"
        else "session-context-lookup.py"
    )
    adapter.run_inner_hook(payload, other_script, runner=runner, marker_dir=tmp_path)
    assert calls == [
        (payload, script),
        ({**payload, "turn_id": "18"}, script),
        (payload, other_script),
    ]


def test_failed_inner_execution_does_not_write_once_marker_and_is_retried(adapter, tmp_path):
    payload = {"conversationId": "conversation-retry"}
    attempts = 0

    def runner(_payload, _script):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("inner hook failed")
        return {}

    with pytest.raises(RuntimeError, match="inner hook failed"):
        adapter.run_inner_hook(payload, "session-start.py", runner=runner, marker_dir=tmp_path)
    assert not list(tmp_path.iterdir())
    adapter.run_inner_hook(payload, "session-start.py", runner=runner, marker_dir=tmp_path)
    assert attempts == 2
    assert any(tmp_path.iterdir())


def test_concurrent_identical_hooks_allow_exactly_one_durable_inner_run(adapter, tmp_path):
    payload = {"conversationId": "conversation-concurrent"}
    first_runner_started = threading.Event()
    release_runner = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def runner(_payload, _script):
        nonlocal calls
        with calls_lock:
            calls += 1
        first_runner_started.set()
        assert release_runner.wait(timeout=3.0)
        return {"stored": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            adapter.run_inner_hook,
            payload,
            "session-start.py",
            runner=runner,
            marker_dir=tmp_path,
        )
        assert first_runner_started.wait(timeout=1.0)
        try:
            claims = list(tmp_path.glob("*.claim"))
            assert len(claims) == 1
            assert re.fullmatch(r"[0-9a-f]{64}\.claim", claims[0].name)
            claim_bytes = claims[0].read_bytes()
            for sensitive in ("conversation-concurrent", "session-start.py"):
                assert sensitive.encode() not in claim_bytes

            second = pool.submit(
                adapter.run_inner_hook,
                payload,
                "session-start.py",
                runner=runner,
                marker_dir=tmp_path,
            )
            try:
                assert second.result(timeout=1.0) == {}
            except FutureTimeoutError:
                pytest.fail("concurrent duplicate entered the durable runner")
        finally:
            release_runner.set()
        assert first.result(timeout=1.0) == {"stored": True}

    assert calls == 1
    assert not list(tmp_path.glob("*.claim"))
    assert len(list(tmp_path.glob("*.done"))) == 1


def test_crashed_claim_owner_retries_without_aba_duplicate(adapter, tmp_path):
    marker_root = tmp_path / "markers"
    runner_log = tmp_path / "runner.log"
    child = f"""\
import importlib.util
import os
import sys
import time
from pathlib import Path

adapter_path = Path({str(ADAPTER_PATH)!r})
marker_root = Path({str(marker_root)!r})
runner_log = Path({str(runner_log)!r})
label = sys.argv[1]
hold = sys.argv[2] == "hold"
spec = importlib.util.spec_from_file_location("agy_claim_owner_" + label, adapter_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def runner(_payload, _script):
    descriptor = os.open(runner_log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, (label + "\\n").encode())
    finally:
        os.close(descriptor)
    if hold:
        time.sleep(60)
    return {{}}

module.run_inner_hook(
    {{"conversationId": "conversation-crash-aba"}},
    "session-start.py",
    runner=runner,
    marker_dir=marker_root,
    claim_stale_after=0.0,
)
"""

    def start(label, *, hold):
        return subprocess.Popen(
            [sys.executable, "-c", child, label, "hold" if hold else "return"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

    def lines():
        if not runner_log.exists():
            return []
        return runner_log.read_text(encoding="utf-8").splitlines()

    def wait_for_line(label):
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if label in lines():
                return
            time.sleep(0.01)
        pytest.fail(f"runner {label!r} did not start; observed {lines()!r}")

    processes = []
    try:
        crashed_owner = start("crashed-owner", hold=True)
        processes.append(crashed_owner)
        wait_for_line("crashed-owner")
        crashed_owner.kill()
        crashed_owner.wait(timeout=2.0)

        retry_a = start("retry-a", hold=True)
        processes.append(retry_a)
        wait_for_line("retry-a")
        claims = list(marker_root.glob("*.claim"))
        assert len(claims) == 1
        assert re.fullmatch(r"[0-9a-f]{64}\.claim", claims[0].name)
        claim_bytes = claims[0].read_bytes()
        for sensitive in (
            "conversation-crash-aba",
            "session-start.py",
            "crashed-owner",
            "retry-a",
        ):
            assert sensitive.encode() not in claim_bytes

        retry_b = start("retry-b", hold=False)
        processes.append(retry_b)
        _, retry_b_stderr = retry_b.communicate(timeout=2.0)
        assert retry_b.returncode == 0, retry_b_stderr

        retry_a.kill()
        retry_a.wait(timeout=2.0)

        retry_c = start("retry-c", hold=False)
        processes.append(retry_c)
        _, retry_c_stderr = retry_c.communicate(timeout=2.0)
        assert retry_c.returncode == 0, retry_c_stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass

    assert lines() == ["crashed-owner", "retry-a", "retry-c"]
    assert len(list(marker_root.glob("*.done"))) == 1
    assert not list(marker_root.glob("*.claim"))


def test_sync_session_end_runs_only_after_fully_idle(adapter, tmp_path):
    busy = {"conversationId": "conversation-sync", "fullyIdle": False}
    idle = {"conversationId": "conversation-sync", "fullyIdle": True}
    calls = []

    def runner(inner_payload, script):
        calls.append((inner_payload, script))
        return {"synced": True}

    assert (
        adapter.run_inner_hook(
            busy,
            "sync-session-to-graph.py",
            runner=runner,
            marker_dir=tmp_path,
            session_end=True,
        )
        == {}
    )
    assert calls == []
    adapter.run_inner_hook(
        idle,
        "sync-session-to-graph.py",
        runner=runner,
        marker_dir=tmp_path,
        session_end=True,
    )
    assert calls == [(idle, "sync-session-to-graph.py")]


def test_default_marker_root_is_antigravity_namespaced_without_writing_home(adapter):
    root = (
        adapter.default_marker_root()
        if hasattr(adapter, "default_marker_root")
        else adapter.DEFAULT_MARKER_ROOT
    )

    assert root == Path.home() / ".cognee-plugin" / "antigravity" / "adapter-once"


def test_marker_never_leaks_prompt_turn_or_script_into_filename_or_content(adapter, tmp_path):
    payload = {
        "conversationId": "conversation-secret",
        "prompt": "private customer incident",
        "turn_id": "turn-secret",
    }

    adapter.run_inner_hook(
        payload, "store-user-prompt.py", runner=lambda *_: {}, marker_dir=tmp_path
    )

    markers = list(tmp_path.rglob("*.done"))
    assert len(markers) == 1
    sensitive_values = (
        "conversation-secret",
        "private customer incident",
        "turn-secret",
        "store-user-prompt.py",
    )
    for marker in markers:
        relative_path = marker.relative_to(tmp_path)
        assert relative_path.parts == (marker.name,)
        assert re.fullmatch(r"[0-9a-f]{64}\.done", marker.name)
        marker_text = marker.read_text(encoding="utf-8")
        for sensitive in sensitive_values:
            assert sensitive not in str(relative_path)
            assert sensitive not in marker_text


def test_run_inner_hook_uses_default_marker_root_under_isolated_home(
    adapter_loader, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    adapter = adapter_loader()
    calls = []

    adapter.run_inner_hook(
        {"conversationId": "default-root"},
        "session-start.py",
        runner=lambda payload, script: calls.append((payload, script)) or {},
    )

    expected_root = home / ".cognee-plugin" / "antigravity" / "adapter-once"
    markers = list(expected_root.glob("*.done"))
    assert calls == [({"conversationId": "default-root"}, "session-start.py")]
    assert len(markers) == 1
    assert [path for path in home.rglob("*") if path.is_file()] == markers


def test_adapter_subprocess_normalizes_payload_and_translates_inner_stdout(adapter, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    adapter_copy = scripts / "agy_hook.py"
    shutil.copy2(ADAPTER_PATH, adapter_copy)
    observed = tmp_path / "observed.json"
    (scripts / "session-context-lookup.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['OBSERVED_PAYLOAD']).write_text(sys.stdin.read(), encoding='utf-8')\n"
        "print(json.dumps({'hookSpecificOutput': {'additionalContext': 'recalled context'}}))\n",
        encoding="utf-8",
    )
    transcript = _write_transcript(
        tmp_path / "smoke.jsonl",
        {
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": "smoke prompt",
            "step_index": 7,
        },
    )
    home = tmp_path / "home"
    home.mkdir()

    result = subprocess.run(
        [sys.executable, str(adapter_copy), "session-context-lookup.py"],
        input=json.dumps(
            {
                "conversationId": "smoke-conversation",
                "workspacePaths": ["/tmp/smoke-workspace"],
                "transcriptPath": str(transcript),
                "modelName": "smoke-model",
            }
        ),
        text=True,
        capture_output=True,
        cwd=scripts,
        env={
            **{key: value for key, value in os.environ.items() if key != "COGNEE_ENV_FILE"},
            "HOME": str(home),
            "USERPROFILE": str(home),
            "OBSERVED_PAYLOAD": str(observed),
        },
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"injectSteps": [{"ephemeralMessage": "recalled context"}]}
    observed_payload = json.loads(observed.read_text(encoding="utf-8"))
    assert observed_payload == {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "smoke-conversation",
        "cwd": "/tmp/smoke-workspace",
        "transcript_path": str(transcript),
        "model": "smoke-model",
        "prompt": "smoke prompt",
        "turn_id": "7",
    }
