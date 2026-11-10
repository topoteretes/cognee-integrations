"""Coding memory capture (SDK-1336 review fixes).

* QA entries are paired by transcript unit (one per real prompt), so a late
  Stop (Claude's Stop hook is async, Codex's is detached) still sends its own
  question with its own answer and prompt time.
* Values are sanitized whole before any byte cap cuts a ``<private>`` span open.
* Cloud coding entries are counted once and request no every-N improve.

``_coding_memory`` is identical in Claude Code and Codex, so these run on Claude
Code only; the Codex rollout tests call ``parse_codex`` directly.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from utils.coding_memory import SESSION_KEY as KEY
from utils.coding_memory import (
    codex_answer,
    codex_item,
    codex_prompt,
    git,
    said,
    tool,
    user,
    write_records,
)


def _commit(repo: Path, text: str) -> str:
    (repo / "README.md").write_text(text + "\n")
    git(repo, "commit", "-qam", text)
    return git(repo, "rev-parse", "HEAD")


def _begin(cm, monkeypatch, prompt: str, at: str, repo: Path) -> None:
    """The prompt hook, running ``at`` (a second after the prompt record)."""
    monkeypatch.setattr(cm, "now_iso", lambda: at)
    cm.begin_turn(KEY, prompt, str(repo))


def _qa(entries: list) -> list:
    return [e for e in entries if e["type"] == "qa"]


def _ctx(qa: dict) -> dict:
    return json.loads(qa["context"])


def _traces(entries: list) -> list:
    return [e for e in entries if e["type"] == "trace"]


def _failed_bash(uid: str, tool_id: str, command: str, output: str, stamp: str) -> list:
    return tool(uid, tool_id, "Bash", {"command": command}, output, stamp, error=True)


def _payload(path: Path, repo: Path, answer: str = "") -> dict:
    payload = {"transcript_path": str(path), "cwd": str(repo)}
    if answer:
        payload["last_assistant_message"] = answer
    return payload


A_PROMPT = "2026-11-05T10:00:00.000Z"
B_PROMPT = "2026-11-05T10:05:00.000Z"


def test_late_stop_sends_each_request_with_its_own_answer(cm, sent, tmp_path, repo, monkeypatch):
    first = git(repo, "rev-parse", "HEAD")
    _begin(cm, monkeypatch, "Request A", "2026-11-05T10:00:01+00:00", repo)
    second = _commit(repo, "after A")
    _begin(cm, monkeypatch, "Request B", "2026-11-05T10:05:01+00:00", repo)
    records = [
        user("ua", "Request A", A_PROMPT),
        said("aa", "Answer A", "2026-11-05T10:01:00.000Z"),
        user("ub", "Request B", B_PROMPT),
    ]
    path = write_records(tmp_path / "s.jsonl", records)
    # A's Stop runs only now, after B's prompt.
    cm.capture(_payload(path, repo, "Answer A"))
    write_records(path, records + [said("ab", "Answer B", "2026-11-05T10:06:00.000Z")])
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
        user("ua", "Request A", A_PROMPT),
        said("aa", "Answer A", "2026-11-05T10:01:00.000Z"),
        user("ub", "Request B", B_PROMPT),
        said("ab1", "Looking at the parser first.", "2026-11-05T10:05:10.000Z"),
    ]
    path = write_records(tmp_path / "s.jsonl", records)
    cm.capture(_payload(path, repo, "Answer A"))
    assert [q["question"] for q in _qa(sent)] == ["Request A"]
    sent.clear()
    write_records(path, records + [said("ab2", "Answer B", "2026-11-05T10:06:00.000Z")])
    cm.capture(_payload(path, repo, "Answer B"))
    (qa,) = _qa(sent)
    assert qa["answer"] == "Answer B" and qa["prompt_time"] == "2026-11-05T10:05:00+00:00"
    assert not _ctx(qa).get("continuation")


def test_two_requests_with_the_same_answer_send_two_qas(cm, sent, tmp_path, repo):
    records = [user("ua", "Rename x", A_PROMPT), said("aa", "Done.", "2026-11-05T10:01:00Z")]
    path = write_records(tmp_path / "s.jsonl", records)
    cm.capture(_payload(path, repo, "Done."))
    records += [user("ub", "Rename y", B_PROMPT), said("ab", "Done.", "2026-11-05T10:06:00Z")]
    write_records(path, records)
    cm.capture(_payload(path, repo, "Done."))
    assert [(q["question"], q["answer"]) for q in _qa(sent)] == [
        ("Rename x", "Done."),
        ("Rename y", "Done."),
    ]


def test_a_second_stop_with_a_new_answer_is_a_continuation(cm, sent, tmp_path, repo):
    records = [user("ua", "Fix it", A_PROMPT)]
    records += tool("t1", "tool-1", "Bash", {"command": "ls"}, "a", "2026-11-05T10:00:30Z")
    records.append(said("a1", "First.", "2026-11-05T10:01:00Z"))
    path = write_records(tmp_path / "s.jsonl", records)
    cm.capture(_payload(path, repo, "First."))
    sent.clear()
    records += tool("t", "tool-2", "Bash", {"command": "pwd"}, "/x", "2026-11-05T10:02:00Z")
    records.append(said("a2", "Second.", "2026-11-05T10:03:00Z"))
    write_records(path, records)
    cm.capture(_payload(path, repo, "Second."))
    # Only the call made since the first Stop is sent.
    assert [t["method_params"]["command"] for t in _traces(sent)] == ["pwd"]
    (qa,) = _qa(sent)
    assert qa["question"] == "Fix it" and qa["answer"] == "Second."
    assert "prompt_time" not in qa and _ctx(qa)["continuation"] is True
    sent.clear()
    cm.capture(_payload(path, repo, "Second."), final=True)  # SessionEnd: nothing new
    assert sent == []


def test_a_skipped_stop_is_made_up_at_the_next_capture(cm, sent, tmp_path, repo):
    records = [
        user("ua", "Request A", A_PROMPT),
        said("aa", "Answer A", "2026-11-05T10:01:00Z"),
        user("ub", "Request B", B_PROMPT),
        said("ab", "Answer B", "2026-11-05T10:06:00Z"),
    ]
    path = write_records(tmp_path / "s.jsonl", records)
    cm.capture(_payload(path, repo, "Answer B"))  # A's Stop never ran
    qa = _qa(sent)
    assert [q["question"] for q in qa] == ["Request A", "Request B"]
    assert [q["prompt_time"] for q in qa] == [
        "2026-11-05T10:00:00+00:00",
        "2026-11-05T10:05:00+00:00",
    ]


def test_session_end_sends_what_the_stops_missed(cm, sent, tmp_path, repo):
    records = [user("ua", "Request A", A_PROMPT), said("aa", "Answer A", "2026-11-05T10:01Z")]
    path = write_records(tmp_path / "s.jsonl", records)
    cm.capture(_payload(path, repo), final=True)  # SessionEnd carries no final message
    (qa,) = _qa(sent)
    assert qa["question"] == "Request A" and qa["prompt_time"] == "2026-11-05T10:00:00+00:00"


def test_meta_sidechain_and_compact_records_start_no_request(cm, tmp_path):
    records = [
        user("ua", "Request A", A_PROMPT),
        user("m", "<system-reminder>x</system-reminder>", "2026-11-05T10:00:10Z"),
        user("s", "side", "2026-11-05T10:00:20Z", isSidechain=True),
        user("c", "summary", "2026-11-05T10:00:30Z", isCompactSummary=True),
        user("meta", "meta", "2026-11-05T10:00:40Z", isMeta=True),
        said("aa", "Answer A", "2026-11-05T10:01:00Z"),
    ]
    parsed = cm.parse_claude(str(write_records(tmp_path / "s.jsonl", records)))
    assert [(u["id"], u["request"], u["answer"], u["answer_id"]) for u in parsed["units"]] == [
        ("ua", "Request A", "Answer A", "aa")
    ]


def test_last_request_is_the_newest_prompt(cm, tmp_path, monkeypatch):
    # The git context is not asserted here: skip the two git runs per turn.
    monkeypatch.setattr(cm, "git_head", lambda cwd: "")
    monkeypatch.setattr(cm, "git_branch", lambda cwd: "")
    cm.begin_turn(KEY, "first", str(tmp_path))
    cm.begin_turn(KEY, "token <private>s3cret</private> second", str(tmp_path))
    assert cm.last_request(KEY) == "token [private] second"
    for _ in range(cm.MAX_TURNS + 5):
        cm.begin_turn(KEY, "again", str(tmp_path))
    assert len(cm.read_state(KEY)["turns"]) == cm.MAX_TURNS


# -- Codex rollouts --------------------------------------------------------------------


def test_codex_late_stop_sends_each_request_with_its_own_answer(
    cm, sent, tmp_path, repo, monkeypatch
):
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    first = git(repo, "rev-parse", "HEAD")
    _begin(cm, monkeypatch, "Request A", "2026-11-05T10:00:01+00:00", repo)
    second = _commit(repo, "after A")
    _begin(cm, monkeypatch, "Request B", "2026-11-05T10:05:01+00:00", repo)
    records = [
        codex_prompt("Request A", A_PROMPT),
        *codex_answer("Answer A", "2026-11-05T10:01:00.000Z"),
        codex_prompt("Request B", B_PROMPT),
    ]
    path = write_records(tmp_path / "rollout.jsonl", records)
    cm.capture(_payload(path, repo, "Answer A"))
    write_records(path, records + codex_answer("Answer B", "2026-11-05T10:06:00.000Z"))
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


def _cut_by_output_cap():
    output = "x" * 7990 + "<private>TOPSECRET</private>"
    return tool("t", "tool-1", "Bash", {"command": "cat f"}, output, "2026-11-05T10:00:10Z")


def _cut_by_params_cap():
    command = "echo " + "x" * 3990 + "<private>TOPSECRET</private>"
    return tool("t", "tool-1", "Bash", {"command": command}, "ok", "2026-11-05T10:00:10Z")


def _cut_by_error_cap():
    output = "<private>TOPSECRET" + "y" * 600 + "</private> failed"
    return tool(
        "t", "tool-1", "Edit", {"file_path": "a"}, output, "2026-11-05T10:00:10Z", error=True
    )


@pytest.mark.parametrize(
    "call",
    [_cut_by_output_cap, _cut_by_params_cap, _cut_by_error_cap],
    ids=["output", "params", "error"],
)
def test_a_private_span_cut_by_a_cap_is_still_removed(cm, sent, tmp_path, repo, call):
    records = [user("ua", "Go", A_PROMPT), *call()]
    cm.capture(_payload(write_records(tmp_path / "s.jsonl", records), repo))
    (trace,) = _traces(sent)
    assert "TOPSECRET" not in json.dumps(trace)
    if call is _cut_by_error_cap:
        assert trace["status"] == "error" and "failed" in trace["error_message"]


def _claude_failed_deploy(tmp_path):
    records = [user("ua", "Go", A_PROMPT)]
    records += _failed_bash(
        "t1",
        "tool-1",
        "deploy",
        "Exit code 1\n<private>TOPSECRET</private> error: deploy refused",
        "2026-11-05T10:00:10Z",
    )
    return write_records(tmp_path / "s.jsonl", records)


def _codex_failed_make(tmp_path):
    output = "<private>" + "a" * 600 + "TOPSECRET</private> boom"
    records = [
        codex_prompt("Go", A_PROMPT),
        codex_item(
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
    return write_records(tmp_path / "rollout.jsonl", records)


@pytest.mark.parametrize(
    "transcript, expected",
    [
        (_claude_failed_deploy, "[private] error: deploy refused (exit code 1)"),
        (_codex_failed_make, "[private] boom (exit code 2)"),
    ],
    ids=["claude", "codex"],
)
def test_a_private_span_never_reaches_the_error_line(
    cm, sent, tmp_path, repo, monkeypatch, transcript, expected
):
    if transcript is _codex_failed_make:
        monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    cm.capture(_payload(transcript(tmp_path), repo))
    (trace,) = _traces(sent)
    assert trace["status"] == "error"
    assert trace["error_message"] == expected
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


# -- Error lines: the line the server keys pitfalls on --------------------------------------


def test_failing_claude_bash_calls_get_their_own_error_lines(cm, sent, tmp_path, repo):
    records = [user("ua", "Go", A_PROMPT)]
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
    cm.capture(_payload(write_records(tmp_path / "s.jsonl", records), repo))
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
        codex_prompt("Go", A_PROMPT),
        codex_item(
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
    cm.capture(_payload(write_records(tmp_path / "rollout.jsonl", records), repo))
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
    traceback = {"is_error": True, "exit_code": 1, "output": _traceback("KeyError: 'x'")}
    assert "Traceback" not in cm.call_error(traceback).splitlines()[0]


# -- Codex file changes: one call per path ----------------------------------------------------


def _file_change(changes: dict, stamp: str, *, item_id="fc-1", status="completed") -> dict:
    return codex_item(
        {"type": "FileChange", "id": item_id, "changes": changes, "status": status}, stamp
    )


def _update(diff: str = "@@ -1 +1 @@\n-a\n+b\n", move_path=None) -> dict:
    return {"type": "update", "unified_diff": diff, "move_path": move_path}


def test_codex_file_change_to_a_secret_path_sends_only_the_other_file(
    cm, sent, tmp_path, repo, monkeypatch
):
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    records = [
        codex_prompt("Go", A_PROMPT),
        _file_change(
            {".env": _update("+DB_DSN=postgres://u:pw@h/db\n"), "src/app.py": _update()},
            "2026-11-05T10:00:10.000Z",
        ),
    ]
    cm.capture(_payload(write_records(tmp_path / "rollout.jsonl", records), repo))
    (trace,) = _traces(sent)
    assert trace["origin_function"] == "Edit"
    assert trace["method_params"]["file_path"] == "src/app.py"
    assert trace["method_params"]["change"] == "update"
    assert trace["method_params"]["diff"] == "@@ -1 +1 @@\n-a\n+b\n"
    assert "pw@h" not in json.dumps(sent)


def test_codex_rename_into_a_secret_path_is_dropped(cm, sent, tmp_path, repo, monkeypatch):
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    records = [
        codex_prompt("Go", A_PROMPT),
        _file_change(
            {"config.txt": _update(move_path=".env"), "src/a.py": _update()},
            "2026-11-05T10:00:10.000Z",
        ),
    ]
    cm.capture(_payload(write_records(tmp_path / "rollout.jsonl", records), repo))
    # The other file of the same change is the control: it is sent.
    assert [t["method_params"]["file_path"] for t in _traces(sent)] == ["src/a.py"]


def test_codex_two_file_change_is_sent_once_per_file(cm, sent, tmp_path, repo, monkeypatch):
    monkeypatch.setattr(cm, "parse_transcript", cm.parse_codex)
    records = [
        codex_prompt("Go", A_PROMPT),
        _file_change(
            {"src/a.py": _update(), "src/b.py": {"type": "delete", "content": "old\n"}},
            "2026-11-05T10:00:10.000Z",
        ),
    ]
    path = write_records(tmp_path / "rollout.jsonl", records)
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
        codex_prompt("Go", A_PROMPT),
        _file_change(
            {"src/new.py": {"type": "add", "content": "print(1)\n"}},
            "2026-11-05T10:00:10.000Z",
            status="failed",
        ),
    ]
    (call,) = cm.parse_codex(str(write_records(tmp_path / "rollout.jsonl", records)))["calls"]
    assert call["name"] == "Write" and call["is_error"] is True
    assert call["input"] == {"file_path": "src/new.py", "change": "add", "content": "print(1)\n"}
    assert call["time"] == "2026-11-05T10:00:10.000Z"


# -- Interrupted requests ----------------------------------------------------------------------


def _interrupt_record(uid: str, text: str, stamp: str) -> dict:
    return user(uid, [{"type": "text", "text": text}], stamp)


@pytest.mark.parametrize(
    "marker", ["[Request interrupted by user]", "[Request interrupted by user for tool use]"]
)
def test_an_interrupted_request_is_sent_closed_at_the_interrupt(cm, sent, tmp_path, repo, marker):
    records = [user("ua", "Request A", A_PROMPT)]
    records += _failed_bash(
        "t1", "tool-1", "make", "Exit code 2\nmake: *** error", "2026-11-05T10:00:10Z"
    )
    records += [
        said("aa", "Looking at the Makefile.", "2026-11-05T10:00:20Z"),
        _interrupt_record("ux", marker, "2026-11-05T10:00:30Z"),
        user("ub", "Request B", B_PROMPT),
        said("ab", "Answer B", "2026-11-05T10:06:00Z"),
    ]
    path = write_records(tmp_path / "s.jsonl", records)
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
    write_records(path, records + [said("ac", "Answer B, revised", "2026-11-05T10:07:00Z")])
    cm.capture(_payload(path, repo, "Answer B, revised"))
    assert [q["question"] for q in _qa(sent)] == ["Request B"]  # A is never continued


def test_an_interrupt_before_any_text_sends_the_prefix_alone(cm, sent, tmp_path, repo):
    records = [
        user("ua", "Request A", A_PROMPT),
        _interrupt_record("ux", "[Request interrupted by user]", "2026-11-05T10:00:30Z"),
    ]
    path = write_records(tmp_path / "s.jsonl", records)
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
        codex_prompt("Request A", A_PROMPT),
        codex_item(
            {"type": "AgentMessage", "id": "m1", "content": [{"type": "Text", "text": "Looking."}]},
            "2026-11-05T10:00:20.000Z",
        ),
        _codex_aborted("2026-11-05T10:00:30.000Z", reason),
        codex_prompt("Request B", B_PROMPT),
        *codex_answer("Answer B", "2026-11-05T10:06:00.000Z"),
    ]
    path = write_records(tmp_path / "rollout.jsonl", records)
    cm.capture(_payload(path, repo, "Answer B"))
    first, second = _qa(sent)
    assert first["question"] == "Request A"
    assert first["answer"] == cm.INTERRUPTED_ANSWER + "\nLooking."
    assert _ctx(first)["interrupted"] is True
    assert (second["question"], second["answer"]) == ("Request B", "Answer B")
    assert "interrupted" not in _ctx(second)

    sent.clear()
    write_records(path, records + codex_answer("Answer B, revised", "2026-11-05T10:07:00.000Z"))
    cm.capture(_payload(path, repo, "Answer B, revised"))
    assert [q["question"] for q in _qa(sent)] == ["Request B"]  # A is never continued


# -- Error lines -------------------------------------------------------------------------------


def _traceback(exception: str) -> str:
    return (
        "Traceback (most recent call last):\n"
        '  File "job.py", line 3, in <module>\n'
        "    main()\n"
        f"{exception}\n"
    )


_NPM_LOG = "npm error A complete log of this run can be found in: /u/.npm/_logs/x-debug-0.log\n"


@pytest.mark.parametrize(
    "output, expected",
    [
        pytest.param(
            _traceback("FileNotFoundError: [Errno 2] No such file or directory: 'data.csv'"),
            "FileNotFoundError: [Errno 2] No such file or directory: 'data.csv'",
            id="traceback-file-not-found",
        ),
        pytest.param(_traceback("KeyError: 'user_id'"), "KeyError: 'user_id'", id="traceback-key"),
        pytest.param(
            _traceback("KeyError: 'x'")
            + "\nDuring handling of the above exception, another exception occurred:\n\n"
            + _traceback("app.errors.ConfigError: config missing"),
            "app.errors.ConfigError: config missing",
            id="chained-traceback-ends-the-program",
        ),
        pytest.param(
            "from cognee.exceptions import CogneeApiError\n"
            "    except Exception as exc:\n"
            "        raise ValueError(exc)\n"
            "bash: nosuchtool: command not found\n",
            "bash: nosuchtool: command not found",
            id="code-printed-before-a-failure",
        ),
        pytest.param(
            "a.py:3:1: F401 [*] `os` imported but unused\nFound 1 error.\n",
            "a.py:3:1: F401 [*] `os` imported but unused",
            id="ruff-rule-not-summary",
        ),
        pytest.param(
            "b.py:9:89: E501 Line too long (120 > 88)\nFound 1 error.\n",
            "b.py:9:89: E501 Line too long (120 > 88)",
            id="ruff-long-line",
        ),
        pytest.param(
            "Found 2 errors.\nsomething failed here\n",
            "something failed here",
            id="ruff-summary-is-no-error-line",
        ),
        pytest.param(
            "   Compiling error-chain v0.12.4\nerror[E0425]: cannot find value `x`\n",
            "error[E0425]: cannot find value `x`",
            id="cargo-progress",
        ),
        pytest.param(
            'npm error Missing script: "tset"\nnpm error\n' + _NPM_LOG,
            'npm error Missing script: "tset"',
            id="npm-first-error-not-log-path",
        ),
        pytest.param(
            "npm error code ENOENT\nnpm error syscall open\n" + _NPM_LOG,
            "npm error code ENOENT",
            id="npm-enoent",
        ),
        pytest.param(
            "npm ERR! code E404\nnpm ERR! 404 Not Found\n", "npm ERR! code E404", id="npm-err"
        ),
        pytest.param(
            "lint failed in src/a.js\nsee the log for details\nbuild failed\n",
            "lint failed in src/a.js",
            id="loose-fallback-first-not-closing",
        ),
        pytest.param(
            "DeprecationWarning: pkg_resources is deprecated\nfatal: bad revision 'xyz'\n",
            "fatal: bad revision 'xyz'",
            id="warning-before-a-failure",
        ),
        # A warning raised as an exception (python -W error) still ends its traceback.
        pytest.param(
            _traceback("DeprecationWarning: old api"),
            "DeprecationWarning: old api",
            id="warning-raised-as-exception",
        ),
        pytest.param(
            "tests/t.py F\n>       assert f() == 2\n"
            "E       assert 1 == 2\n=== 1 failed in 0.1s ===\n",
            "E       assert 1 == 2",
            id="pytest-e-line-without-error-word",
        ),
    ],
)
def test_the_error_line_is_the_line_that_names_the_failure(cm, output, expected):
    assert cm._error_line(output) == expected


# -- Repository root ---------------------------------------------------------------------------


def test_a_qa_from_a_subdirectory_carries_the_repository_root(cm, sent, tmp_path, repo):
    sub = repo / "pkg" / "sub"
    sub.mkdir(parents=True)
    records = [user("ua", "Go", A_PROMPT), said("aa", "Done.", "2026-11-05T10:01:00Z")]
    cm.capture(_payload(write_records(tmp_path / "s.jsonl", records), sub, "Done."))
    context = _ctx(_qa(sent)[0])
    # Native separators: git prints C:/... on Windows, and git_root normalises it.
    assert context["cwd"] == os.path.normpath(git(repo, "rev-parse", "--show-toplevel"))
    assert context["session_cwd"] == str(sub)


def test_outside_git_the_qa_cwd_is_the_directory(cm, sent, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    records = [user("ua", "Go", A_PROMPT), said("aa", "Done.", "2026-11-05T10:01:00Z")]
    cm.capture(_payload(write_records(tmp_path / "s.jsonl", records), plain, "Done."))
    context = _ctx(_qa(sent)[0])
    assert context["cwd"] == context["session_cwd"] == str(plain)


# -- Forked sessions ---------------------------------------------------------------------------


FORK = {"sessionId": "parent-session", "messageUuid": "aa"}


def _forked(records: list) -> list:
    return [{**record, "sessionId": "fork-session", "forkedFrom": FORK} for record in records]


def test_a_forked_claude_session_sends_only_its_own_records(cm, sent, tmp_path, repo, monkeypatch):
    parent = [user("ua", "Request A", A_PROMPT)]
    parent += tool("t1", "tool-1", "Bash", {"command": "ls"}, "a.txt", "2026-11-05T10:00:10Z")
    parent.append(said("aa", "Answer A", "2026-11-05T10:01:00Z"))
    head = git(repo, "rev-parse", "HEAD")
    _begin(cm, monkeypatch, "Request B", "2026-11-05T10:05:01+00:00", repo)
    own = [user("ub", "Request B", B_PROMPT)]
    own += tool("t2", "tool-2", "Bash", {"command": "pwd"}, "/x", "2026-11-05T10:05:10Z")
    own.append(said("ab", "Answer B", "2026-11-05T10:06:00Z"))
    path = write_records(tmp_path / "fork.jsonl", _forked(parent) + own)

    cm.capture(_payload(path, repo, "Answer B"))
    assert [t["method_params"]["command"] for t in _traces(sent)] == ["pwd"]
    (qa,) = _qa(sent)
    assert (qa["question"], qa["answer"]) == ("Request B", "Answer B")
    assert _ctx(qa)["start_commit"] == head and _ctx(qa)["end_commit"] == head

    # Without forkedFrom the same records are this session's own history.
    plain = cm.parse_claude(str(write_records(tmp_path / "plain.jsonl", parent + own)))
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
    return write_records(
        tmp_path / "rollout.jsonl",
        [
            _codex_meta(
                "2026-11-05T10:04:00.000Z",
                forked_from_id="thread-1",
                history_mode=history_mode,
            ),
            # Copied from the parent thread, with their original times.
            codex_prompt("Request A", A_PROMPT),
            codex_item({**command, "id": "exec-1"}, "2026-11-05T10:00:10.000Z"),
            *codex_answer("Answer A", "2026-11-05T10:01:00.000Z"),
            # The fork's own events.
            codex_prompt("Request B", B_PROMPT),
            codex_item({**command, "id": "exec-2"}, "2026-11-05T10:05:10.000Z"),
            *codex_answer("Answer B", "2026-11-05T10:06:00.000Z"),
        ],
    )


@pytest.mark.parametrize(
    "history_mode, calls, units",
    [
        # A legacy fork copies the parent's events: only those after the fork count.
        ("legacy", ["exec-2"], [("Request B", "Answer B")]),
        ("paginated", ["exec-1", "exec-2"], [("Request A", "Answer A"), ("Request B", "Answer B")]),
    ],
)
def test_a_codex_fork_parses_by_its_history_mode(cm, tmp_path, history_mode, calls, units):
    parsed = cm.parse_codex(str(_codex_fork_rollout(tmp_path, history_mode)))
    assert [c["id"] for c in parsed["calls"]] == calls
    assert [(u["request"], u["answer"]) for u in parsed["units"]] == units
