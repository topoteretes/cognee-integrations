"""Coding memory mode (COGNEE_CODING_MEMORY=true, SDK-1336).

Capture at the end of each request, one lessons block per session, git tags and
the failure behaviour, for the Claude Code and Codex plugins (``_coding_memory``
is shared verbatim between them).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

CODING_SUITES = {"claude-code", "codex"}


@pytest.fixture
def cm(suite, isolated_modules, monkeypatch):
    if suite.name not in CODING_SUITES:
        pytest.skip("coding memory ships for Claude Code and Codex only")
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    module = isolated_modules(suite, "_coding_memory")
    common = isolated_modules(suite, "_plugin_common")
    common.set_session_key("host-session-1")
    monkeypatch.setattr(module, "_session", lambda: ("claude_s1", "agent_sessions"))
    monkeypatch.setattr(module, "server_supported", lambda: True)
    return module


@pytest.fixture
def sent(cm, monkeypatch):
    """Entries handed to the send path, in order (no network, no buffer)."""
    entries: list = []
    monkeypatch.setattr(cm, "_deliver", lambda dataset, sid, batch: entries.extend(batch))
    monkeypatch.setattr(cm, "_drain", lambda *a: None)
    monkeypatch.setattr(cm, "_maybe_cooldown_improve", lambda *a: None)
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
    (root / "README.md").write_text("hello\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / "src" / "old.py").write_text("y = 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


# -- Transcripts --------------------------------------------------------------------


def _claude_line(kind: str, content, stamp: str, **extra) -> str:
    return json.dumps(
        {"type": kind, "timestamp": stamp, "message": {"role": kind, "content": content}, **extra}
    )


def _claude_transcript(
    path: Path, calls: list, *, request="Fix the parser", answer="Done."
) -> Path:
    lines = [_claude_line("user", request, "2026-11-05T10:00:00.000Z")]
    for index, (tool_id, name, tool_input, output, is_error) in enumerate(calls):
        use = f"2026-11-05T10:00:{10 + 2 * index:02d}.000Z"
        result = f"2026-11-05T10:00:{11 + 2 * index:02d}.000Z"
        lines.append(
            _claude_line(
                "assistant",
                [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
                use,
            )
        )
        lines.append(
            _claude_line(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": output,
                        "is_error": is_error,
                    }
                ],
                result,
            )
        )
    lines.append(
        _claude_line("assistant", [{"type": "text", "text": answer}], "2026-11-05T10:01:00.000Z")
    )
    path.write_text("\n".join(lines) + "\n")
    return path


def _codex_event(item: dict, stamp: str) -> str:
    return json.dumps(
        {
            "timestamp": stamp,
            "type": "event_msg",
            "payload": {"type": "item_completed", "turn_id": "t1", "item": item},
        }
    )


def _codex_transcript(path: Path) -> Path:
    lines = [
        _codex_event(
            {"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": "Fix it"}]},
            "2026-11-05T10:00:00.000Z",
        ),
        _codex_event(
            {
                "type": "CommandExecution",
                "id": "exec-1",
                "command": ["/bin/zsh", "-lc", "ls src"],
                "aggregated_output": "app.py\n",
                "exit_code": 0,
                "status": "completed",
            },
            "2026-11-05T10:00:05.000Z",
        ),
        _codex_event(
            {
                "type": "CommandExecution",
                "id": "exec-2",
                "command": ["/bin/zsh", "-lc", "pytest -q"],
                "aggregated_output": "1 failed",
                "exit_code": 1,
                "status": "failed",
            },
            "2026-11-05T10:00:09.000Z",
        ),
        _codex_event(
            {
                "type": "FileChange",
                "id": "fc-1",
                "changes": {
                    "src/app.py": {
                        "type": "update",
                        "unified_diff": "@@ -1 +1 @@\n-x = 1\n+x = 2\n",
                        "move_path": None,
                    }
                },
                "status": "completed",
            },
            "2026-11-05T10:00:12.000Z",
        ),
        json.dumps(
            {
                "timestamp": "2026-11-05T10:01:00.000Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": "Fixed."},
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def _transcript(cm, tmp_path: Path, calls=None) -> Path:
    if cm.agent() == "codex":
        return _codex_transcript(tmp_path / "rollout.jsonl")
    calls = calls or [
        ("t1", "Bash", {"command": "ls src"}, "app.py", False),
        ("t2", "Read", {"file_path": "src/app.py"}, "x = 1", False),
        ("t3", "Edit", {"file_path": "src/app.py"}, "String to replace not found", True),
    ]
    return _claude_transcript(tmp_path / "session.jsonl", calls)


# -- Setting --------------------------------------------------------------------------


def test_setting_unset_keeps_every_hook_as_before(
    suite, hook_module, isolated_modules, monkeypatch
):
    if suite.name not in CODING_SUITES:
        pytest.skip("coding memory ships for Claude Code and Codex only")
    monkeypatch.delenv("COGNEE_CODING_MEMORY", raising=False)
    config = isolated_modules(suite, "config")
    assert config.coding_memory_enabled() is False
    send_unit = hook_module(suite, "send-unit.py")
    monkeypatch.setattr(send_unit, "_capture", lambda payload: pytest.fail("captured while off"))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"session_id": "x"}'))
    send_unit.main()  # exits before reading anything


def test_hooks_register_the_stop_capture_and_codex_compaction(suite):
    if suite.name not in CODING_SUITES:
        pytest.skip("coding memory ships for Claude Code and Codex only")
    hooks = json.loads(suite.hooks_json.read_text())["hooks"]
    stop = [h["command"] for group in hooks["Stop"] for h in group["hooks"]]
    assert any("send-unit.py" in c for c in stop)
    if suite.name == "codex":
        assert "compact" in hooks["SessionStart"][0]["matcher"].split("|")


# -- Capture -------------------------------------------------------------------------


def test_capture_sends_every_call_including_failures_and_reads(
    cm, sent, tmp_path, repo, monkeypatch
):
    path = _transcript(cm, tmp_path)
    request = "Fix it" if cm.agent() == "codex" else "Fix the parser"
    # The prompt hook runs a second after the transcript's prompt record.
    monkeypatch.setattr(cm, "now_iso", lambda: "2026-11-05T10:00:01+00:00")
    cm.begin_turn("host-session-1", request, str(repo))
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "second")
    end = _git(repo, "rev-parse", "HEAD")

    cm.capture({"transcript_path": str(path), "cwd": str(repo), "last_assistant_message": "Done."})

    traces = [e for e in sent if e["type"] == "trace"]
    qa = [e for e in sent if e["type"] == "qa"]
    if cm.agent() == "codex":
        assert [t["origin_function"] for t in traces] == ["Bash", "Bash", "Edit"]
        assert traces[0]["method_params"]["command"] == "ls src"
        assert traces[1]["status"] == "error" and "exit code 1" in traces[1]["error_message"]
    else:
        assert [t["origin_function"] for t in traces] == ["Bash", "Read", "Edit"]
        assert traces[2]["status"] == "error"
        assert "not found" in traces[2]["error_message"]
    assert all(t["time"].endswith("+00:00") for t in traces)
    assert len(qa) == 1
    assert qa[0]["question"] == request and qa[0]["answer"] == "Done."
    assert qa[0]["prompt_time"] and qa[0]["time"]
    context = json.loads(qa[0]["context"])
    assert context["start_commit"] == head and context["end_commit"] == end
    assert context["branch"] == "main" and context["date"] == qa[0]["prompt_time"][:10]
    assert context["transcript_path"] == str(path) and context["cwd"] == str(repo)


def test_second_stop_sends_only_new_calls_as_a_continuation(cm, sent, tmp_path, repo):
    if cm.agent() == "codex":
        pytest.skip("the Claude transcript fixture grows call by call")
    first = [("t1", "Bash", {"command": "ls"}, "a", False)]
    path = _claude_transcript(tmp_path / "s.jsonl", first, request="Fix it")
    cm.begin_turn("host-session-1", "Fix it", str(repo))
    cm.capture({"transcript_path": str(path), "cwd": str(repo), "last_assistant_message": "One."})
    sent.clear()
    _claude_transcript(
        path, first + [("t2", "Bash", {"command": "pwd"}, "/x", False)], request="Fix it"
    )
    cm.capture({"transcript_path": str(path), "cwd": str(repo), "last_assistant_message": "Two."})
    traces = [e for e in sent if e["type"] == "trace"]
    assert [t["method_params"]["command"] for t in traces] == ["pwd"]
    qa = [e for e in sent if e["type"] == "qa"]
    assert qa[0]["question"] == "Fix it" and "prompt_time" not in qa[0]
    assert json.loads(qa[0]["context"])["continuation"] is True


def test_same_answer_twice_sends_no_second_qa(cm, sent, tmp_path, repo):
    path = _transcript(cm, tmp_path)
    payload = {"transcript_path": str(path), "cwd": str(repo), "last_assistant_message": "Done."}
    cm.capture(payload)
    sent.clear()
    cm.capture(payload, final=True)  # SessionEnd after the last Stop
    assert sent == []


def test_subagent_calls_are_read_from_their_own_files(cm, sent, tmp_path, repo):
    if cm.agent() == "codex":
        pytest.skip("Claude Code writes subagent transcripts beside the session")
    path = _claude_transcript(
        tmp_path / "s.jsonl", [("t1", "Agent", {"prompt": "look"}, "ok", False)]
    )
    sub = tmp_path / "s" / "subagents"
    sub.mkdir(parents=True)
    _claude_transcript(
        sub / "agent-a1.jsonl", [("s1", "Grep", {"pattern": "def"}, "app.py", False)], answer="sub"
    )
    cm.capture({"transcript_path": str(path), "cwd": str(repo), "last_assistant_message": "Done."})
    names = [e["origin_function"] for e in sent if e["type"] == "trace"]
    assert names == ["Agent", "Grep"] or sorted(names) == ["Agent", "Grep"]


def test_secrets_and_private_spans_are_removed(cm, sent, tmp_path, repo):
    if cm.agent() == "codex":
        pytest.skip("one transcript format is enough for the shared sanitizer")
    calls = [("t1", "Bash", {"command": "echo <private>hunter2</private>"}, "ok", False)]
    prompt = "token <private>s3cret</private> please"
    path = _claude_transcript(tmp_path / "s.jsonl", calls, request=prompt)
    cm.begin_turn("host-session-1", prompt, str(repo))
    cm.capture({"transcript_path": str(path), "cwd": str(repo), "last_assistant_message": "ok"})
    text = json.dumps(sent)
    assert "hunter2" not in text and "s3cret" not in text and "[private]" in text


def test_plugin_invocations_are_never_sent(cm, sent, tmp_path, repo):
    if cm.agent() == "codex":
        pytest.skip("one transcript format is enough for the shared filter")
    command = "bash ~/.claude/plugins/cognee/scripts/cognee-search.sh foo"
    path = _claude_transcript(
        tmp_path / "s.jsonl", [("t1", "Bash", {"command": command}, "x", False)]
    )
    cm.capture({"transcript_path": str(path), "cwd": str(repo), "last_assistant_message": "ok"})
    assert [e for e in sent if e["type"] == "trace"] == []


def test_capture_is_off_when_the_server_is_too_old(cm, sent, tmp_path, repo, monkeypatch):
    monkeypatch.setattr(cm, "server_supported", lambda: False)
    path = _transcript(cm, tmp_path)
    assert cm.capture({"transcript_path": str(path), "cwd": str(repo)}) == {
        "skipped": "server_unsupported"
    }
    assert sent == []


def test_cloud_mode_spools_and_local_mode_buffers(cm, suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    buffered, spooled = [], []
    monkeypatch.setattr(common, "append_warmup_entry", lambda d, s, e: buffered.append(e))
    entries = [{"type": "trace", "origin_function": "Bash"}, {"type": "qa", "question": "q"}]
    monkeypatch.setattr(cm, "_cloud_spool", lambda: False)
    cm._deliver("agent_sessions", "claude_s1", entries)
    assert buffered == entries
    if suite.name != "claude-code":
        return
    worker = isolated_modules(suite, "_cloud_worker")
    monkeypatch.setattr(
        worker, "enqueue", lambda d, s, e, kind, coding=False: spooled.append((kind, coding))
    )
    monkeypatch.setattr(cm, "_cloud_spool", lambda: True)
    cm._deliver("agent_sessions", "claude_s1", entries)
    assert spooled == [("trace", True), ("answer", True)]


def test_server_support_reads_the_entry_schemas(cm, suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    spec = {
        "components": {
            "schemas": {
                "QAEntry": {"properties": {"time": {}, "prompt_time": {}}},
                "TraceEntry": {"properties": {"time": {}}},
            }
        }
    }
    monkeypatch.setattr(common, "_json_http_request", lambda *a, **k: spec)
    assert cm.check_server_support(refresh=True)["ok"] is True
    old = {"components": {"schemas": {"QAEntry": {"properties": {}}, "TraceEntry": {}}}}
    monkeypatch.setattr(common, "_json_http_request", lambda *a, **k: old)
    assert cm.check_server_support(refresh=True)["ok"] is False

    def down(*a, **k):
        raise OSError("refused")

    monkeypatch.setattr(common, "_json_http_request", down)
    # A failed forced check says "could not tell"; the cached verdict stays for
    # the callers within its hour.
    assert cm.check_server_support(refresh=True)["ok"] is None
    assert cm.check_server_support()["ok"] is False


# -- Selection -----------------------------------------------------------------------


def _lesson(body, *, kind="other", key="", date="2026-11-01", session="s", created=""):
    return {
        "data_id": body[:8],
        "lesson_type": kind,
        "state_key": key,
        "session_id": session,
        "session_date": date,
        "git_commit": "",
        "files": [],
        "created_at": created,
        "body": body,
    }


def _newest_first(lessons):
    return sorted(
        lessons, key=lambda item: (item["session_date"], item["created_at"]), reverse=True
    )


def test_only_the_newer_test_baseline_is_used(cm):
    lessons = _newest_first(
        [
            _lesson("Tests: 10 pass.", kind="state", key="test-baseline", date="2026-11-01"),
            _lesson("Tests: 12 pass.", kind="state", key="test-baseline", date="2026-11-03"),
        ]
    )
    assert [item["body"] for item in cm.select_lessons(lessons, "")] == ["Tests: 12 pass."]


def test_only_the_two_newest_pitfalls_are_used(cm):
    lessons = _newest_first(
        [
            _lesson(
                f"Pitfall {n} → use: x",
                kind="pitfall",
                key=f"pitfall:Bash:{n}",
                date=f"2026-11-0{n}",
            )
            for n in (1, 2, 3)
        ]
    )
    assert [item["body"] for item in cm.select_lessons(lessons, "")] == [
        "Pitfall 3 → use: x",
        "Pitfall 2 → use: x",
    ]


def test_recalled_lessons_follow_in_recall_order_and_duplicates_are_skipped(cm):
    lessons = _newest_first(
        [
            _lesson("Parser lives in src/parse.py.", date="2026-11-02"),
            _lesson("parser   LIVES in src/parse.py.", date="2026-11-01"),
            _lesson("CLI entry is cli/main.py.", date="2026-11-03"),
        ]
    )
    recalled = ["cli entry is cli/main.py.", "Parser lives in src/parse.py."]
    chosen = [item["body"] for item in cm.select_lessons(lessons, recalled)]
    assert chosen == ["CLI entry is cli/main.py.", "Parser lives in src/parse.py."]


def test_an_oversized_lesson_is_skipped_and_a_later_shorter_one_kept(cm):
    big = _lesson("B" * 1400, kind="pitfall", key="pitfall:a", date="2026-11-03")
    bigger = _lesson("C" * 200, kind="pitfall", key="pitfall:b", date="2026-11-02")
    small = _lesson("Short recalled fact.", date="2026-11-01")
    lessons = _newest_first([big, bigger, small])
    chosen = cm.select_lessons(lessons, ["short recalled fact."])
    assert [item["body"][:5] for item in chosen] == ["BBBBB", "Short"]


def test_lesson_documents_parse_headers_title_and_statement(cm):
    text = (
        "Lesson-Type: pitfall\nState-Key: pitfall:Edit:x:py\n"
        "# Session learning (session claude_1)\n\n"
        "Edit fails → use: a script. (seen in 2 sessions)\n"
    )
    lesson = cm.parse_lesson_text(text)
    assert lesson == {
        "lesson_type": "pitfall",
        "state_key": "pitfall:Edit:x:py",
        "session_id": "claude_1",
        "body": "Edit fails → use: a script. (seen in 2 sessions)",
    }
    assert cm.is_lesson(text) and not cm.is_lesson("just a document")


# -- Delivery -------------------------------------------------------------------------


def test_the_block_is_given_once_per_session_and_again_after_compaction(cm, monkeypatch):
    calls = []
    monkeypatch.setattr(cm, "build_block", lambda q, cwd: calls.append(q) or "BLOCK")
    assert cm.deliver("host-session-1", "first", "/x") == "BLOCK"
    assert cm.deliver("host-session-1", "second", "/x") == ""
    cm.reset_delivery("host-session-1")
    assert cm.deliver("host-session-1", "third", "/x") == "BLOCK"
    assert calls == ["first", "third"]


def test_a_failed_lookup_is_retried_on_the_next_prompt(cm, monkeypatch):
    def boom(q, cwd):
        raise TimeoutError("slow server")

    monkeypatch.setattr(cm, "build_block", boom)
    assert cm.deliver("host-session-1", "first", "/x") == ""
    monkeypatch.setattr(cm, "build_block", lambda q, cwd: "BLOCK")
    assert cm.deliver("host-session-1", "second", "/x") == "BLOCK"


def test_build_block_lists_lessons_of_this_project_and_renders(
    cm, suite, isolated_modules, monkeypatch, repo
):
    common = isolated_modules(suite, "_plugin_common")
    rows = [
        {
            "id": "11111111-0000-0000-0000-000000000001",
            "createdAt": "2026-11-03T10:00:00Z",
            "externalMetadata": {
                "lesson_type": "state",
                "state_key": "test-baseline",
                "session_id": "claude_old",
                "session_date": "2026-11-03",
                "git_commit": _git(repo, "rev-parse", "HEAD"),
                "files": ["src/app.py"],
                "project_node_sets": ["proj-A"],
            },
        },
        {
            "id": "11111111-0000-0000-0000-000000000002",
            "externalMetadata": {"lesson_type": "location", "project_node_sets": ["proj-B"]},
        },
        {"id": "11111111-0000-0000-0000-000000000003", "externalMetadata": {}},
    ]
    bodies = {
        rows[0]["id"]: "Lesson-Type: state\nState-Key: test-baseline\n"
        "# Session learning (session claude_old)\n\nAll 12 tests pass.\n",
        rows[1]["id"]: "Lesson-Type: location\n# Session learning (session x)\n\nOther repo.\n",
    }

    def request(path, payload=None, **kwargs):
        if path.startswith("/api/v1/datasets/ds-1/data"):
            return rows
        if path == "/api/v1/recall":
            return []
        raise AssertionError(path)

    monkeypatch.setattr(common, "_json_http_request", request)
    monkeypatch.setattr(common, "dataset_id_for", lambda name, *a: "ds-1")
    monkeypatch.setattr(cm, "_raw_text", lambda ds, data_id, timeout: bodies[data_id])
    monkeypatch.setattr(cm, "project_scope", lambda *a: ["proj-A"])
    (repo / "src" / "app.py").write_text("x = 3\n")
    _git(repo, "commit", "-qam", "change app")

    block = cm.build_block("run the tests", str(repo))
    assert block.startswith(
        "Project memory (Cognee), learned from 1 earlier session in the agent_sessions dataset."
    )
    assert "- All 12 tests pass. (session 2026-11-03, commit " in block
    assert "src/app.py changed since)" in block
    assert "Other repo" not in block
    assert "Recent commits (newest first):" in block and "change app" not in block
    assert len(block) < 2300


def test_no_lessons_means_no_block(cm, suite, isolated_modules, monkeypatch, repo):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "_json_http_request", lambda *a, **k: [])
    monkeypatch.setattr(common, "dataset_id_for", lambda name, *a: "ds-1")
    assert cm.build_block("anything", str(repo)) == ""


def test_server_down_or_404_recall_gives_no_recalled_lessons(
    cm, suite, isolated_modules, monkeypatch
):
    common = isolated_modules(suite, "_plugin_common")

    def down(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(common, "_json_http_request", down)
    assert cm.recall_passages("q", "agent_sessions", "ds-1", 10**12) == []


# -- Git tags ---------------------------------------------------------------------------


def test_tags_name_changed_and_deleted_files_case_insensitively(cm, repo):
    commit = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("changed\n")
    (repo / "src" / "old.py").unlink()
    _git(repo, "commit", "-qam", "edit readme, drop old")
    lesson = {
        "git_commit": commit,
        "session_date": "2026-11-03",
        "files": ["readme.md", "old.py", "app.py", "never.py"],
    }
    tag = cm.lesson_tag(lesson, str(repo))
    short = _git(repo, "rev-parse", "--short", commit)
    assert (
        tag
        == f"(session 2026-11-03, commit {short}; readme.md changed since; old.py no longer exists)"
    )


def test_unknown_commit_and_no_commit_get_no_tag(cm, repo):
    assert (
        cm.lesson_tag({"git_commit": "deadbeefdeadbeef", "files": ["README.md"]}, str(repo)) == ""
    )
    assert cm.lesson_tag({"git_commit": "", "files": ["README.md"]}, str(repo)) == ""


def test_more_than_two_paths_of_a_kind_are_counted(cm, repo):
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text("1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    commit = _git(repo, "rev-parse", "HEAD")
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text("2\n")
    tag = cm.lesson_tag({"git_commit": commit, "files": ["a.py", "b.py", "c.py"]}, str(repo))
    assert tag.endswith("a.py, b.py +1 more changed since)")


def test_recent_commits_line_format(cm, repo):
    (repo / "src" / "app.py").write_text("x = 1\ny = 2\n")
    _git(repo, "commit", "-qam", "grow")
    lines = cm.recent_commits(str(repo))
    assert len(lines) == 2
    assert lines[0].endswith("src/app.py (+1/−0 lines)")
    assert "(+3/−0 lines)" in lines[1] and "+1 more" in lines[1]


def test_outside_git_there_are_no_tags_and_no_commits(cm, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cm.git_root(str(plain)) == ""
    assert cm.recent_commits(str(plain)) == []
    lesson = _lesson("A fact.")
    lesson["git_commit"] = "abc1234"
    block = cm.render_block("ds", 2, [lesson], str(plain), in_git=False)
    assert (
        "- A fact." in block
        and "Recent commits" not in block
        and "commit" not in block.split("Lessons:")[1]
    )


# -- Codex rollout -------------------------------------------------------------------------


def test_codex_rollout_maps_items_to_tool_calls(cm, tmp_path):
    parsed = cm.parse_codex(str(_codex_transcript(tmp_path / "r.jsonl")))
    assert parsed["request"] == "Fix it" and parsed["answer"] == "Fixed."
    assert [(c["name"], c["is_error"]) for c in parsed["calls"]] == [
        ("Bash", False),
        ("Bash", True),
        ("Edit", False),
    ]
    edit = parsed["calls"][2]
    assert edit["id"] == "fc-1:src/app.py"
    assert edit["input"]["file_path"] == "src/app.py" and edit["input"]["change"] == "update"
    assert edit["input"]["diff"].startswith("@@")
