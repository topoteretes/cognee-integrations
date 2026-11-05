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
    assert trace["error_message"].startswith("exit code 2: ")
    assert trace["error_message"].endswith("boom")
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
