"""Coding memory capture (SDK-1336 review fixes).

* QA entries are paired by transcript unit (one per real prompt), so a late
  Stop (Claude's Stop hook is async, Codex's is detached) still sends its own
  question with its own answer and prompt time.
* Values are sanitized whole before any byte cap cuts a ``<private>`` span open.
* Cloud coding entries are counted once and request no every-N improve.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

CODING_SUITES = {"claude-code", "codex"}
KEY = "host-session-1"


@pytest.fixture
def cm(suite, isolated_modules, monkeypatch):
    if suite.name not in CODING_SUITES:
        pytest.skip("coding memory ships for Claude Code and Codex only")
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    module = isolated_modules(suite, "_coding_memory")
    common = isolated_modules(suite, "_plugin_common")
    common.set_session_key(KEY)
    monkeypatch.setattr(module, "_session", lambda: ("claude_s1", "agent_sessions"))
    monkeypatch.setattr(module, "server_supported", lambda: True)
    return module


@pytest.fixture
def sent(cm, monkeypatch):
    entries: list = []
    monkeypatch.setattr(cm, "_deliver", lambda dataset, sid, batch: entries.extend(batch))
    monkeypatch.setattr(cm, "_drain", lambda *a: None)
    monkeypatch.setattr(cm, "_maybe_cooldown_improve", lambda *a: None)
    # Claude transcripts by default in both suites; the Codex tests switch it.
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_claude)
    return entries


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


def _commit(repo: Path, text: str) -> str:
    (repo / "a.txt").write_text(text + "\n")
    _git(repo, "commit", "-qam", text)
    return _git(repo, "rev-parse", "HEAD")


def _begin(cm, monkeypatch, prompt: str, at: str, repo: Path) -> None:
    """The prompt hook, running ``at`` (a second after the prompt record)."""
    monkeypatch.setattr(cm, "now_iso", lambda: at)
    cm.begin_turn(KEY, prompt, str(repo))


# -- Claude transcripts ---------------------------------------------------------------


def _user(uid: str, text, stamp: str, **extra) -> dict:
    return {
        "type": "user",
        "uuid": uid,
        "timestamp": stamp,
        "message": {"role": "user", "content": text},
        **extra,
    }


def _said(uid: str, text: str, stamp: str) -> dict:
    return {
        "type": "assistant",
        "uuid": uid,
        "timestamp": stamp,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _tool(uid: str, tool_id: str, name: str, tool_input, output, stamp: str, *, error=False):
    use = {
        "type": "assistant",
        "uuid": uid + "-use",
        "timestamp": stamp,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
        },
    }
    result = _user(
        uid + "-result",
        [{"type": "tool_result", "tool_use_id": tool_id, "content": output, "is_error": error}],
        stamp,
    )
    return [use, result]


def _write(path: Path, records: list) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _qa(entries: list) -> list:
    return [e for e in entries if e["type"] == "qa"]


def _ctx(qa: dict) -> dict:
    return json.loads(qa["context"])


def _payload(path: Path, repo: Path, answer: str = "") -> dict:
    payload = {"transcript_path": str(path), "cwd": str(repo)}
    if answer:
        payload["last_assistant_message"] = answer
    return payload


A_PROMPT = "2026-11-05T10:00:00.000Z"
B_PROMPT = "2026-11-05T10:05:00.000Z"


def test_late_stop_sends_each_request_with_its_own_answer(cm, sent, tmp_path, repo, monkeypatch):
    first = _git(repo, "rev-parse", "HEAD")
    _begin(cm, monkeypatch, "Request A", "2026-11-05T10:00:01+00:00", repo)
    second = _commit(repo, "after A")
    _begin(cm, monkeypatch, "Request B", "2026-11-05T10:05:01+00:00", repo)
    records = [
        _user("ua", "Request A", A_PROMPT),
        _said("aa", "Answer A", "2026-11-05T10:01:00.000Z"),
        _user("ub", "Request B", B_PROMPT),
    ]
    path = _write(tmp_path / "s.jsonl", records)
    # A's Stop runs only now, after B's prompt.
    cm.capture(_payload(path, repo, "Answer A"))
    _write(path, records + [_said("ab", "Answer B", "2026-11-05T10:06:00.000Z")])
    cm.capture(_payload(path, repo, "Answer B"))

    qa = _qa(sent)
    assert [(q["question"], q["answer"]) for q in qa] == [
        ("Request A", "Answer A"),
        ("Request B", "Answer B"),
    ]
    assert qa[0]["prompt_time"] == "2026-11-05T10:00:00+00:00"
    assert qa[1]["prompt_time"] == "2026-11-05T10:05:00+00:00"
    assert qa[0]["time"] == "2026-11-05T10:01:00+00:00"
    assert not any(_ctx(q).get("continuation") for q in qa)
    assert _ctx(qa[0])["start_commit"] == first and _ctx(qa[0])["end_commit"] == second
    assert _ctx(qa[1])["start_commit"] == second


def test_late_stop_leaves_a_running_request_alone(cm, sent, tmp_path, repo):
    records = [
        _user("ua", "Request A", A_PROMPT),
        _said("aa", "Answer A", "2026-11-05T10:01:00.000Z"),
        _user("ub", "Request B", B_PROMPT),
        _said("ab1", "Looking at the parser first.", "2026-11-05T10:05:10.000Z"),
    ]
    path = _write(tmp_path / "s.jsonl", records)
    cm.capture(_payload(path, repo, "Answer A"))
    assert [q["question"] for q in _qa(sent)] == ["Request A"]
    sent.clear()
    _write(path, records + [_said("ab2", "Answer B", "2026-11-05T10:06:00.000Z")])
    cm.capture(_payload(path, repo, "Answer B"))
    (qa,) = _qa(sent)
    assert qa["answer"] == "Answer B" and qa["prompt_time"] == "2026-11-05T10:05:00+00:00"
    assert not _ctx(qa).get("continuation")


def test_two_requests_with_the_same_answer_send_two_qas(cm, sent, tmp_path, repo):
    records = [_user("ua", "Rename x", A_PROMPT), _said("aa", "Done.", "2026-11-05T10:01:00Z")]
    path = _write(tmp_path / "s.jsonl", records)
    cm.capture(_payload(path, repo, "Done."))
    records += [_user("ub", "Rename y", B_PROMPT), _said("ab", "Done.", "2026-11-05T10:06:00Z")]
    _write(path, records)
    cm.capture(_payload(path, repo, "Done."))
    assert [(q["question"], q["answer"]) for q in _qa(sent)] == [
        ("Rename x", "Done."),
        ("Rename y", "Done."),
    ]


def test_a_second_stop_with_a_new_answer_is_a_continuation(cm, sent, tmp_path, repo):
    records = [_user("ua", "Fix it", A_PROMPT), _said("a1", "First.", "2026-11-05T10:01:00Z")]
    path = _write(tmp_path / "s.jsonl", records)
    cm.capture(_payload(path, repo, "First."))
    sent.clear()
    records += _tool("t", "tool-2", "Bash", {"command": "pwd"}, "/x", "2026-11-05T10:02:00Z")
    records.append(_said("a2", "Second.", "2026-11-05T10:03:00Z"))
    _write(path, records)
    cm.capture(_payload(path, repo, "Second."))
    (qa,) = _qa(sent)
    assert qa["question"] == "Fix it" and qa["answer"] == "Second."
    assert "prompt_time" not in qa and _ctx(qa)["continuation"] is True
    sent.clear()
    cm.capture(_payload(path, repo, "Second."), final=True)  # SessionEnd: nothing new
    assert sent == []


def test_a_skipped_stop_is_made_up_at_the_next_capture(cm, sent, tmp_path, repo):
    records = [
        _user("ua", "Request A", A_PROMPT),
        _said("aa", "Answer A", "2026-11-05T10:01:00Z"),
        _user("ub", "Request B", B_PROMPT),
        _said("ab", "Answer B", "2026-11-05T10:06:00Z"),
    ]
    path = _write(tmp_path / "s.jsonl", records)
    cm.capture(_payload(path, repo, "Answer B"))  # A's Stop never ran
    qa = _qa(sent)
    assert [q["question"] for q in qa] == ["Request A", "Request B"]
    assert [q["prompt_time"] for q in qa] == [
        "2026-11-05T10:00:00+00:00",
        "2026-11-05T10:05:00+00:00",
    ]


def test_session_end_sends_what_the_stops_missed(cm, sent, tmp_path, repo):
    records = [_user("ua", "Request A", A_PROMPT), _said("aa", "Answer A", "2026-11-05T10:01Z")]
    path = _write(tmp_path / "s.jsonl", records)
    cm.capture(_payload(path, repo), final=True)  # SessionEnd carries no final message
    (qa,) = _qa(sent)
    assert qa["question"] == "Request A" and qa["prompt_time"] == "2026-11-05T10:00:00+00:00"


def test_meta_sidechain_and_compact_records_start_no_request(cm, tmp_path):
    records = [
        _user("ua", "Request A", A_PROMPT),
        _user("m", "<system-reminder>x</system-reminder>", "2026-11-05T10:00:10Z"),
        _user("s", "side", "2026-11-05T10:00:20Z", isSidechain=True),
        _user("c", "summary", "2026-11-05T10:00:30Z", isCompactSummary=True),
        _user("meta", "meta", "2026-11-05T10:00:40Z", isMeta=True),
        _said("aa", "Answer A", "2026-11-05T10:01:00Z"),
    ]
    parsed = cm.parse_claude(str(_write(tmp_path / "s.jsonl", records)))
    assert [(u["id"], u["request"], u["answer"], u["answer_id"]) for u in parsed["units"]] == [
        ("ua", "Request A", "Answer A", "aa")
    ]
    assert parsed["request"] == "Request A" and parsed["answer"] == "Answer A"


def test_last_request_is_the_newest_prompt(cm, repo):
    cm.begin_turn(KEY, "first", str(repo))
    cm.begin_turn(KEY, "token <private>s3cret</private> second", str(repo))
    assert cm.last_request(KEY) == "token [private] second"
    for _ in range(cm.MAX_TURNS + 5):
        cm.begin_turn(KEY, "again", str(repo))
    assert len(cm.read_state(KEY)["turns"]) == cm.MAX_TURNS


# -- Codex rollouts --------------------------------------------------------------------


def _codex_item(item: dict, stamp: str) -> dict:
    return {
        "timestamp": stamp,
        "type": "event_msg",
        "payload": {"type": "item_completed", "turn_id": "t", "item": item},
    }


def _codex_prompt(text: str, stamp: str) -> dict:
    return _codex_item(
        {"type": "UserMessage", "id": "u", "content": [{"type": "text", "text": text}]}, stamp
    )


def _codex_answer(text: str, stamp: str) -> list:
    return [
        _codex_item(
            {"type": "AgentMessage", "id": "m", "content": [{"type": "Text", "text": text}]},
            stamp,
        ),
        {
            "timestamp": stamp,
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": text},
        },
    ]


def test_codex_late_stop_sends_each_request_with_its_own_answer(
    cm, sent, tmp_path, repo, monkeypatch
):
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    first = _git(repo, "rev-parse", "HEAD")
    _begin(cm, monkeypatch, "Request A", "2026-11-05T10:00:01+00:00", repo)
    second = _commit(repo, "after A")
    _begin(cm, monkeypatch, "Request B", "2026-11-05T10:05:01+00:00", repo)
    records = [
        _codex_prompt("Request A", A_PROMPT),
        *_codex_answer("Answer A", "2026-11-05T10:01:00.000Z"),
        _codex_prompt("Request B", B_PROMPT),
    ]
    path = _write(tmp_path / "rollout.jsonl", records)
    cm.capture(_payload(path, repo, "Answer A"))
    _write(path, records + _codex_answer("Answer B", "2026-11-05T10:06:00.000Z"))
    cm.capture(_payload(path, repo, "Answer B"))
    cm.capture(_payload(path, repo), final=True)  # nothing new at SessionEnd

    qa = _qa(sent)
    assert [(q["question"], q["answer"]) for q in qa] == [
        ("Request A", "Answer A"),
        ("Request B", "Answer B"),
    ]
    assert [q["prompt_time"] for q in qa] == [
        "2026-11-05T10:00:00+00:00",
        "2026-11-05T10:05:00+00:00",
    ]
    assert not any(_ctx(q).get("continuation") for q in qa)
    assert _ctx(qa[0])["start_commit"] == first and _ctx(qa[1])["start_commit"] == second


# -- Sanitize before slicing -------------------------------------------------------------


def test_a_private_span_cut_by_the_output_cap_is_still_removed(cm, sent, tmp_path, repo):
    output = "x" * 7990 + "<private>TOPSECRET</private>"
    records = [_user("ua", "Go", A_PROMPT)]
    records += _tool("t", "tool-1", "Bash", {"command": "cat f"}, output, "2026-11-05T10:00:10Z")
    path = _write(tmp_path / "s.jsonl", records)
    cm.capture(_payload(path, repo))
    (trace,) = [e for e in sent if e["type"] == "trace"]
    assert "TOPSECRET" not in json.dumps(trace)


def test_a_private_span_cut_by_the_params_cap_is_still_removed(cm, sent, tmp_path, repo):
    command = "echo " + "x" * 3990 + "<private>TOPSECRET</private>"
    records = [_user("ua", "Go", A_PROMPT)]
    records += _tool("t", "tool-1", "Bash", {"command": command}, "ok", "2026-11-05T10:00:10Z")
    cm.capture(_payload(_write(tmp_path / "s.jsonl", records), repo))
    (trace,) = [e for e in sent if e["type"] == "trace"]
    assert "TOPSECRET" not in json.dumps(trace)


def test_a_private_span_cut_by_the_error_cap_is_still_removed(cm, sent, tmp_path, repo):
    output = "<private>TOPSECRET" + "y" * 600 + "</private> failed"
    records = [_user("ua", "Go", A_PROMPT)]
    records += _tool("t", "tool-1", "Edit", {"file_path": "a"}, output, "2026-11-05T10:00:10Z")
    records[-1]["message"]["content"][0]["is_error"] = True
    cm.capture(_payload(_write(tmp_path / "s.jsonl", records), repo))
    (trace,) = [e for e in sent if e["type"] == "trace"]
    assert trace["status"] == "error" and "failed" in trace["error_message"]
    assert "TOPSECRET" not in json.dumps(trace)


def test_codex_failed_command_error_tail_keeps_no_private_text(
    cm, sent, tmp_path, repo, monkeypatch
):
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    output = "<private>" + "a" * 600 + "TOPSECRET</private> boom"
    records = [
        _codex_prompt("Go", A_PROMPT),
        _codex_item(
            {
                "type": "CommandExecution",
                "id": "exec-1",
                "command": ["/bin/zsh", "-lc", "make"],
                "aggregated_output": output,
                "exit_code": 2,
                "status": "failed",
            },
            "2026-11-05T10:00:10.000Z",
        ),
    ]
    cm.capture(_payload(_write(tmp_path / "rollout.jsonl", records), repo))
    (trace,) = [e for e in sent if e["type"] == "trace"]
    assert trace["status"] == "error"
    assert trace["error_message"] == "[private] boom (exit code 2)"
    assert "TOPSECRET" not in json.dumps(trace)


# -- Cloud counting -----------------------------------------------------------------------


@pytest.fixture
def cloud(cm, suite, isolated_modules, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("the cloud worker ships with Claude Code only")
    worker = isolated_modules(suite, "_cloud_worker")
    monkeypatch.setenv("COGNEE_SESSION_KEY", KEY)
    monkeypatch.setenv("COGNEE_AUTO_IMPROVE_EVERY", "150")
    monkeypatch.setattr(
        worker, "target", lambda: {"base_url": "https://cloud.example", "principal": "key"}
    )
    monkeypatch.setattr(worker, "ensure_worker", lambda: None)
    monkeypatch.setattr(worker, "log", lambda *a, **kw: None)
    return worker


def _wanted(worker) -> list:
    return [worker.read(p)["reason"] for p in (worker.root() / "improve-wanted").glob("*.json")]


def _drain_all(worker) -> None:
    while list((worker.root() / "spool").glob("*.json")):
        assert worker.drain(lambda item: {"entry_id": "saved"}) > 0


def _common():
    # The copy the loaded modules import; isolated_modules would reset the env.
    return importlib.import_module("_plugin_common")


def test_cloud_coding_entries_count_once_and_request_no_every_n_improve(cm, cloud, monkeypatch):
    common = _common()
    monkeypatch.setattr(cm, "_cloud_spool", lambda: True)
    entries = [{"type": "trace", "origin_function": "Bash", "n": i} for i in range(200)]
    cm._deliver("agent_sessions", "claude_s1", entries)
    assert common.read_turn_count("claude_s1") == 0  # counted on delivery only
    _drain_all(cloud)
    assert common.read_turn_count("claude_s1") == 200
    assert not any(reason.startswith("turn_") for reason in _wanted(cloud))


def test_cloud_normal_entries_still_request_the_every_n_improve(cm, cloud):
    common = _common()
    for i in range(150):
        cloud.enqueue("agent_sessions", "claude_s1", {"type": "trace", "n": i}, "trace")
    _drain_all(cloud)
    assert common.read_turn_count("claude_s1") == 150
    assert _wanted(cloud) == ["turn_150"]


def test_local_coding_entries_bump_the_counter_once_each(cm, suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "append_warmup_entry", lambda d, s, e: None)
    monkeypatch.setattr(cm, "_cloud_spool", lambda: False)
    cm._deliver("agent_sessions", "claude_s1", [{"type": "trace"}] * 3 + [{"type": "qa"}])
    assert common.read_turn_count("claude_s1") == 4


# -- Error lines: the line the server keys pitfalls on --------------------------------------


def _traces(entries: list) -> list:
    return [e for e in entries if e["type"] == "trace"]


def _failed_bash(uid: str, tool_id: str, command: str, output: str, stamp: str) -> list:
    return _tool(uid, tool_id, "Bash", {"command": command}, output, stamp, error=True)


def test_failing_claude_bash_calls_get_their_own_error_lines(cm, sent, tmp_path, repo):
    records = [_user("ua", "Go", A_PROMPT)]
    records += _failed_bash(
        "t1",
        "tool-1",
        "pytest -q",
        "Exit code 1\n..F\nFAILED tests/test_api.py::test_login - AssertionError: 401 != 200\n"
        "1 failed, 2 passed in 0.2s",
        "2026-11-05T10:00:10Z",
    )
    records += _failed_bash(
        "t2",
        "tool-2",
        "ls nope",
        "Exit code 2\nls: nope: No such file or directory",
        "2026-11-05T10:00:20Z",
    )
    cm.capture(_payload(_write(tmp_path / "s.jsonl", records), repo))
    first, second = _traces(sent)
    lines = [t["error_message"].splitlines()[0] for t in (first, second)]
    assert lines == [
        "FAILED tests/test_api.py::test_login - AssertionError: 401 != 200 (exit code 1)",
        "ls: nope: No such file or directory (exit code 2)",
    ]
    assert not any(line.lower().startswith("exit code") for line in lines)
    assert first["error_message"].splitlines()[1:] == [
        "..F",
        "FAILED tests/test_api.py::test_login - AssertionError: 401 != 200",
        "1 failed, 2 passed in 0.2s",
    ]
    assert first["method_return_value"].startswith("..F")


def test_codex_long_failure_keeps_a_whole_error_line(cm, sent, tmp_path, repo, monkeypatch):
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    output = (
        "".join(f"   Compiling crate-{n} v0.1.{n}\n" for n in range(40))
        + "error[E0425]: cannot find value `x` in this scope\n"
        + "".join(f"  --> src/lib.rs:{n}:5 | note {n}\n" for n in range(40))
    )
    assert len(output.encode()) > 2000
    records = [
        _codex_prompt("Go", A_PROMPT),
        _codex_item(
            {
                "type": "CommandExecution",
                "id": "exec-1",
                "command": ["/bin/zsh", "-lc", "cargo build"],
                "aggregated_output": output,
                "exit_code": 101,
                "status": "failed",
            },
            "2026-11-05T10:00:10.000Z",
        ),
    ]
    cm.capture(_payload(_write(tmp_path / "rollout.jsonl", records), repo))
    (trace,) = _traces(sent)
    head, *tail = trace["error_message"].splitlines()
    assert head == "error[E0425]: cannot find value `x` in this scope (exit code 101)"
    assert len(trace["error_message"].encode()) <= cm.MAX_ERROR_BYTES
    assert tail and all(line in output.splitlines() for line in tail)
    assert tail[-1] == "  --> src/lib.rs:39:5 | note 39"


def test_an_output_without_an_error_line_uses_its_first_line(cm):
    call = {"is_error": True, "exit_code": 3, "output": "\n\n  build step one\nstep two\n"}
    assert cm.call_error(call) == "build step one (exit code 3)\n  build step one\nstep two"
    assert cm.call_error({"is_error": True, "exit_code": 4, "output": ""}) == "exit code 4"
    assert cm.call_error({"is_error": True, "exit_code": 1, "output": "E   assert 1 == 2"}) == (
        "E   assert 1 == 2 (exit code 1)"
    )


def test_a_private_span_never_reaches_the_error_line(cm, sent, tmp_path, repo):
    records = [_user("ua", "Go", A_PROMPT)]
    records += _failed_bash(
        "t1",
        "tool-1",
        "deploy",
        "Exit code 1\n<private>TOPSECRET</private> error: deploy refused",
        "2026-11-05T10:00:10Z",
    )
    cm.capture(_payload(_write(tmp_path / "s.jsonl", records), repo))
    (trace,) = _traces(sent)
    assert trace["error_message"] == "[private] error: deploy refused (exit code 1)"
    assert "TOPSECRET" not in json.dumps(trace)


# -- Codex file changes: one call per path ----------------------------------------------------


def _file_change(changes: dict, stamp: str, *, item_id="fc-1", status="completed") -> dict:
    return _codex_item(
        {"type": "FileChange", "id": item_id, "changes": changes, "status": status}, stamp
    )


def _update(diff: str = "@@ -1 +1 @@\n-a\n+b\n", move_path=None) -> dict:
    return {"type": "update", "unified_diff": diff, "move_path": move_path}


def test_codex_file_change_to_a_secret_path_sends_only_the_other_file(
    cm, sent, tmp_path, repo, monkeypatch
):
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    records = [
        _codex_prompt("Go", A_PROMPT),
        _file_change(
            {".env": _update("+DB_DSN=postgres://u:pw@h/db\n"), "src/app.py": _update()},
            "2026-11-05T10:00:10.000Z",
        ),
    ]
    cm.capture(_payload(_write(tmp_path / "rollout.jsonl", records), repo))
    (trace,) = _traces(sent)
    assert trace["origin_function"] == "Edit"
    assert trace["method_params"]["file_path"] == "src/app.py"
    assert trace["method_params"]["change"] == "update"
    assert "pw@h" not in json.dumps(sent)


def test_codex_rename_into_a_secret_path_is_dropped(cm, sent, tmp_path, repo, monkeypatch):
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    records = [
        _codex_prompt("Go", A_PROMPT),
        _file_change({"config.txt": _update(move_path=".env")}, "2026-11-05T10:00:10.000Z"),
    ]
    cm.capture(_payload(_write(tmp_path / "rollout.jsonl", records), repo))
    assert _traces(sent) == []


def test_codex_two_file_change_is_sent_once_per_file(cm, sent, tmp_path, repo, monkeypatch):
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    records = [
        _codex_prompt("Go", A_PROMPT),
        _file_change(
            {"src/a.py": _update(), "src/b.py": {"type": "delete", "content": "old\n"}},
            "2026-11-05T10:00:10.000Z",
        ),
    ]
    path = _write(tmp_path / "rollout.jsonl", records)
    cm.capture(_payload(path, repo))
    cm.capture(_payload(path, repo))
    traces = _traces(sent)
    assert [(t["origin_function"], t["method_params"]["file_path"]) for t in traces] == [
        ("Edit", "src/a.py"),
        ("Edit", "src/b.py"),
    ]
    assert traces[1]["method_params"]["change"] == "delete"
    assert traces[1]["method_params"]["content"] == "old\n"
    parsed = cm.parse_codex(str(path))
    assert [c["id"] for c in parsed["calls"]] == ["fc-1:src/a.py", "fc-1:src/b.py"]


def test_codex_added_file_is_a_write_with_its_content(cm, tmp_path):
    records = [
        _codex_prompt("Go", A_PROMPT),
        _file_change(
            {"src/new.py": {"type": "add", "content": "print(1)\n"}},
            "2026-11-05T10:00:10.000Z",
            status="failed",
        ),
    ]
    (call,) = cm.parse_codex(str(_write(tmp_path / "rollout.jsonl", records)))["calls"]
    assert call["name"] == "Write" and call["is_error"] is True
    assert call["input"] == {"file_path": "src/new.py", "change": "add", "content": "print(1)\n"}
    assert call["time"] == "2026-11-05T10:00:10.000Z"


# -- Interrupted requests ----------------------------------------------------------------------


def _interrupt_record(uid: str, text: str, stamp: str) -> dict:
    return _user(uid, [{"type": "text", "text": text}], stamp)


@pytest.mark.parametrize(
    "marker", ["[Request interrupted by user]", "[Request interrupted by user for tool use]"]
)
def test_an_interrupted_request_is_sent_closed_at_the_interrupt(cm, sent, tmp_path, repo, marker):
    records = [_user("ua", "Request A", A_PROMPT)]
    records += _failed_bash(
        "t1", "tool-1", "make", "Exit code 2\nmake: *** error", "2026-11-05T10:00:10Z"
    )
    records += [
        _said("aa", "Looking at the Makefile.", "2026-11-05T10:00:20Z"),
        _interrupt_record("ux", marker, "2026-11-05T10:00:30Z"),
        _user("ub", "Request B", B_PROMPT),
        _said("ab", "Answer B", "2026-11-05T10:06:00Z"),
    ]
    path = _write(tmp_path / "s.jsonl", records)
    parsed = cm.parse_claude(str(path))
    assert [u["id"] for u in parsed["units"]] == ["ua", "ub"]  # the marker is no request

    cm.capture(_payload(path, repo, "Answer B"))
    first, second = _qa(sent)
    assert first["question"] == "Request A"
    assert first["answer"] == cm.INTERRUPTED_ANSWER + "\nLooking at the Makefile."
    assert first["time"] == "2026-11-05T10:00:30+00:00"
    assert _ctx(first)["interrupted"] is True
    (trace,) = _traces(sent)
    assert trace["status"] == "error" and trace["time"] < first["time"]
    assert (second["question"], second["answer"]) == ("Request B", "Answer B")
    assert "interrupted" not in _ctx(second) and second["prompt_time"]

    sent.clear()
    _write(path, records + [_said("ac", "Answer B, revised", "2026-11-05T10:07:00Z")])
    cm.capture(_payload(path, repo, "Answer B, revised"))
    assert [q["question"] for q in _qa(sent)] == ["Request B"]  # A is never continued


def test_an_interrupt_before_any_text_sends_the_prefix_alone(cm, sent, tmp_path, repo):
    records = [
        _user("ua", "Request A", A_PROMPT),
        _interrupt_record("ux", "[Request interrupted by user]", "2026-11-05T10:00:30Z"),
    ]
    path = _write(tmp_path / "s.jsonl", records)
    # No Stop fires on Esc: a Stop's final message never replaces the interrupt.
    cm.capture(_payload(path, repo, "An older final message."))
    (qa,) = _qa(sent)
    assert qa["answer"] == cm.INTERRUPTED_ANSWER and qa["time"] == "2026-11-05T10:00:30+00:00"
    assert _ctx(qa)["interrupted"] is True


def _codex_aborted(stamp: str, reason: str = "interrupted") -> dict:
    return {
        "timestamp": stamp,
        "type": "event_msg",
        "payload": {"type": "turn_aborted", "turn_id": "t", "reason": reason},
    }


@pytest.mark.parametrize("reason", ["interrupted", "replaced"])
def test_an_interrupted_codex_turn_is_sent_closed_at_the_abort(
    cm, sent, tmp_path, repo, monkeypatch, reason
):
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    records = [
        _codex_prompt("Request A", A_PROMPT),
        _codex_item(
            {"type": "AgentMessage", "id": "m1", "content": [{"type": "Text", "text": "Looking."}]},
            "2026-11-05T10:00:20.000Z",
        ),
        _codex_aborted("2026-11-05T10:00:30.000Z", reason),
        _codex_prompt("Request B", B_PROMPT),
        *_codex_answer("Answer B", "2026-11-05T10:06:00.000Z"),
    ]
    path = _write(tmp_path / "rollout.jsonl", records)
    cm.capture(_payload(path, repo, "Answer B"))
    first, second = _qa(sent)
    assert first["question"] == "Request A"
    assert first["answer"] == cm.INTERRUPTED_ANSWER + "\nLooking."
    assert _ctx(first)["interrupted"] is True
    assert (second["question"], second["answer"]) == ("Request B", "Answer B")
    assert "interrupted" not in _ctx(second)

    sent.clear()
    _write(path, records + _codex_answer("Answer B, revised", "2026-11-05T10:07:00.000Z"))
    cm.capture(_payload(path, repo, "Answer B, revised"))
    assert [q["question"] for q in _qa(sent)] == ["Request B"]  # A is never continued


def test_a_codex_turn_aborted_before_any_text_sends_the_prefix_alone(
    cm, sent, tmp_path, repo, monkeypatch
):
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    records = [_codex_prompt("Request A", A_PROMPT), _codex_aborted("2026-11-05T10:00:30.000Z")]
    cm.capture(_payload(_write(tmp_path / "rollout.jsonl", records), repo))
    (qa,) = _qa(sent)
    assert qa["answer"] == cm.INTERRUPTED_ANSWER and _ctx(qa)["interrupted"] is True


# -- Error lines -------------------------------------------------------------------------------


def _traceback(exception: str) -> str:
    return (
        "Traceback (most recent call last):\n"
        '  File "job.py", line 3, in <module>\n'
        "    main()\n"
        f"{exception}\n"
    )


def test_two_python_tracebacks_get_their_own_error_lines(cm):
    missing = cm._error_line(
        _traceback("FileNotFoundError: [Errno 2] No such file or directory: 'data.csv'")
    )
    key = cm._error_line(_traceback("KeyError: 'user_id'"))
    assert missing == "FileNotFoundError: [Errno 2] No such file or directory: 'data.csv'"
    assert key == "KeyError: 'user_id'"
    assert (
        "Traceback"
        not in cm.call_error(
            {"is_error": True, "exit_code": 1, "output": _traceback("KeyError: 'x'")}
        ).splitlines()[0]
    )


def test_a_chained_traceback_gives_the_exception_that_ended_the_program(cm):
    output = (
        _traceback("KeyError: 'x'")
        + "\nDuring handling of the above exception, another exception occurred:\n\n"
        + _traceback("app.errors.ConfigError: config missing")
    )
    assert cm._error_line(output) == "app.errors.ConfigError: config missing"


def test_code_printed_before_a_failure_is_not_its_error_line(cm):
    output = (
        "from cognee.exceptions import CogneeApiError\n"
        "    except Exception as exc:\n"
        "        raise ValueError(exc)\n"
        "bash: nosuchtool: command not found\n"
    )
    assert cm._error_line(output) == "bash: nosuchtool: command not found"


def test_ruff_failures_are_keyed_on_their_rule_not_the_summary(cm):
    unused = cm._error_line("a.py:3:1: F401 [*] `os` imported but unused\nFound 1 error.\n")
    long = cm._error_line("b.py:9:89: E501 Line too long (120 > 88)\nFound 1 error.\n")
    assert unused.startswith("a.py:3:1: F401") and long.startswith("b.py:9:89: E501")
    assert cm._error_line("Found 2 errors.\nsomething failed here\n") == "something failed here"


def test_cargo_progress_does_not_hide_the_compile_error(cm):
    output = "   Compiling error-chain v0.12.4\nerror[E0425]: cannot find value `x`\n"
    assert cm._error_line(output) == "error[E0425]: cannot find value `x`"


def test_npm_failures_are_keyed_on_their_first_error_not_the_log_path(cm):
    log = "npm error A complete log of this run can be found in: /u/.npm/_logs/x-debug-0.log\n"
    missing = cm._error_line('npm error Missing script: "tset"\nnpm error\n' + log)
    enoent = cm._error_line("npm error code ENOENT\nnpm error syscall open\n" + log)
    assert missing == 'npm error Missing script: "tset"'
    assert enoent == "npm error code ENOENT"
    assert cm._error_line("npm ERR! code E404\nnpm ERR! 404 Not Found\n") == "npm ERR! code E404"


def test_the_loose_fallback_takes_the_first_error_word_not_a_closing_line(cm):
    output = "lint failed in src/a.js\nsee the log for details\nbuild failed\n"
    assert cm._error_line(output) == "lint failed in src/a.js"


def test_a_warning_printed_before_a_failure_is_not_its_error_line(cm):
    output = "DeprecationWarning: pkg_resources is deprecated\nfatal: bad revision 'xyz'\n"
    assert cm._error_line(output) == "fatal: bad revision 'xyz'"
    # A warning raised as an exception (python -W error) still ends its traceback.
    assert cm._error_line(_traceback("DeprecationWarning: old api")) == (
        "DeprecationWarning: old api"
    )


def test_a_pytest_e_line_is_picked_without_an_error_word(cm):
    output = (
        "tests/t.py F\n>       assert f() == 2\nE       assert 1 == 2\n=== 1 failed in 0.1s ===\n"
    )
    assert cm._error_line(output) == "E       assert 1 == 2"


# -- Repository root ---------------------------------------------------------------------------


def test_a_qa_from_a_subdirectory_carries_the_repository_root(cm, sent, tmp_path, repo):
    sub = repo / "pkg" / "sub"
    sub.mkdir(parents=True)
    records = [_user("ua", "Go", A_PROMPT), _said("aa", "Done.", "2026-11-05T10:01:00Z")]
    cm.capture(_payload(_write(tmp_path / "s.jsonl", records), sub, "Done."))
    context = _ctx(_qa(sent)[0])
    assert context["cwd"] == _git(repo, "rev-parse", "--show-toplevel")
    assert context["session_cwd"] == str(sub)


def test_outside_git_the_qa_cwd_is_the_directory(cm, sent, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    records = [_user("ua", "Go", A_PROMPT), _said("aa", "Done.", "2026-11-05T10:01:00Z")]
    cm.capture(_payload(_write(tmp_path / "s.jsonl", records), plain, "Done."))
    context = _ctx(_qa(sent)[0])
    assert context["cwd"] == context["session_cwd"] == str(plain)


# -- Forked sessions ---------------------------------------------------------------------------


FORK = {"sessionId": "parent-session", "messageUuid": "aa"}


def _forked(records: list) -> list:
    return [{**record, "sessionId": "fork-session", "forkedFrom": FORK} for record in records]


def test_a_forked_claude_session_sends_only_its_own_records(cm, sent, tmp_path, repo, monkeypatch):
    parent = [_user("ua", "Request A", A_PROMPT)]
    parent += _tool("t1", "tool-1", "Bash", {"command": "ls"}, "a.txt", "2026-11-05T10:00:10Z")
    parent.append(_said("aa", "Answer A", "2026-11-05T10:01:00Z"))
    head = _git(repo, "rev-parse", "HEAD")
    _begin(cm, monkeypatch, "Request B", "2026-11-05T10:05:01+00:00", repo)
    own = [_user("ub", "Request B", B_PROMPT)]
    own += _tool("t2", "tool-2", "Bash", {"command": "pwd"}, "/x", "2026-11-05T10:05:10Z")
    own.append(_said("ab", "Answer B", "2026-11-05T10:06:00Z"))
    path = _write(tmp_path / "fork.jsonl", _forked(parent) + own)

    cm.capture(_payload(path, repo, "Answer B"))
    assert [t["method_params"]["command"] for t in _traces(sent)] == ["pwd"]
    (qa,) = _qa(sent)
    assert (qa["question"], qa["answer"]) == ("Request B", "Answer B")
    assert _ctx(qa)["start_commit"] == head and _ctx(qa)["end_commit"] == head

    # Without forkedFrom the same records are this session's own history.
    plain = cm.parse_claude(str(_write(tmp_path / "plain.jsonl", parent + own)))
    assert [u["id"] for u in plain["units"]] == ["ua", "ub"]
    assert [c["id"] for c in plain["calls"]] == ["tool-1", "tool-2"]


def _codex_meta(stamp: str, **fields) -> dict:
    return {
        "timestamp": stamp,
        "type": "session_meta",
        "payload": {"id": "thread-2", "timestamp": stamp, **fields},
    }


def _codex_fork_rollout(tmp_path: Path, history_mode: str) -> Path:
    command = {
        "type": "CommandExecution",
        "command": ["/bin/zsh", "-lc", "ls"],
        "aggregated_output": "a",
        "exit_code": 0,
        "status": "completed",
    }
    return _write(
        tmp_path / "rollout.jsonl",
        [
            _codex_meta(
                "2026-11-05T10:04:00.000Z",
                forked_from_id="thread-1",
                history_mode=history_mode,
            ),
            # Copied from the parent thread, with their original times.
            _codex_prompt("Request A", A_PROMPT),
            _codex_item({**command, "id": "exec-1"}, "2026-11-05T10:00:10.000Z"),
            *_codex_answer("Answer A", "2026-11-05T10:01:00.000Z"),
            # The fork's own events.
            _codex_prompt("Request B", B_PROMPT),
            _codex_item({**command, "id": "exec-2"}, "2026-11-05T10:05:10.000Z"),
            *_codex_answer("Answer B", "2026-11-05T10:06:00.000Z"),
        ],
    )


def test_a_codex_legacy_fork_parses_only_events_after_the_fork(cm, tmp_path):
    parsed = cm.parse_codex(str(_codex_fork_rollout(tmp_path, "legacy")))
    assert [c["id"] for c in parsed["calls"]] == ["exec-2"]
    assert [(u["request"], u["answer"]) for u in parsed["units"]] == [("Request B", "Answer B")]


def test_a_codex_paginated_fork_parses_as_any_rollout(cm, tmp_path):
    parsed = cm.parse_codex(str(_codex_fork_rollout(tmp_path, "paginated")))
    assert [c["id"] for c in parsed["calls"]] == ["exec-1", "exec-2"]
    assert [u["request"] for u in parsed["units"]] == ["Request A", "Request B"]
