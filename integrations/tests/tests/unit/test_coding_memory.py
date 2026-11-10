"""Coding memory mode (COGNEE_CODING_MEMORY=true, SDK-1336).

Capture at the end of each request, one lessons block per session, git tags and
the failure behaviour, for the Claude Code and Codex plugins (``_coding_memory``
is shared verbatim between them, so most tests run on Claude Code only).
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest
from utils.coding_memory import (
    SESSION_KEY,
    claude_transcript,
    codex_item,
    codex_prompt,
    entry_spec,
    git,
    lesson,
    record_sent,
    require_coding,
)

# -- Transcripts --------------------------------------------------------------------


def _codex_transcript(path: Path) -> Path:
    command = {"type": "CommandExecution", "status": "completed"}
    records = [
        codex_prompt("Fix it", "2026-11-05T10:00:00.000Z"),
        codex_item(
            {
                **command,
                "id": "exec-1",
                "command": ["/bin/zsh", "-lc", "ls src"],
                "aggregated_output": "app.py\n",
                "exit_code": 0,
            },
            "2026-11-05T10:00:05.000Z",
        ),
        codex_item(
            {
                **command,
                "id": "exec-2",
                "command": ["/bin/zsh", "-lc", "pytest -q"],
                "aggregated_output": "1 failed",
                "exit_code": 1,
                "status": "failed",
            },
            "2026-11-05T10:00:09.000Z",
        ),
        codex_item(
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
        {
            "timestamp": "2026-11-05T10:01:00.000Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "Fixed."},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _transcript(cm, tmp_path: Path, calls=None) -> Path:
    if cm.agent() == "codex":
        return _codex_transcript(tmp_path / "rollout.jsonl")
    calls = calls or [
        ("t1", "Bash", {"command": "ls src"}, "app.py", False),
        ("t2", "Read", {"file_path": "src/app.py"}, "x = 1", False),
        ("t3", "Edit", {"file_path": "src/app.py"}, "String to replace not found", True),
    ]
    return claude_transcript(tmp_path / "session.jsonl", calls)


# -- Setting --------------------------------------------------------------------------


def test_setting_unset_keeps_every_hook_as_before(
    suite, hook_module, isolated_modules, monkeypatch
):
    require_coding(suite)
    monkeypatch.delenv("COGNEE_CODING_MEMORY", raising=False)
    config = isolated_modules(suite, "config")
    assert config.coding_memory_enabled() is False
    send_unit = hook_module(suite, "send-unit.py")
    # Codex hands the payload to a detached child (_handoff -> _spawn), Claude
    # captures inline: every way out must stay unused while the setting is off.
    for name in ("_capture", "_handoff", "_spawn", "_run_detached"):
        monkeypatch.setattr(send_unit, name, lambda *a, _n=name: pytest.fail(f"{_n} while off"))
    monkeypatch.setattr(
        send_unit._coding_memory, "warm_lessons", lambda *a: pytest.fail("warmed while off")
    )
    for argv in (["send-unit.py"], ["send-unit.py", "--run", "x"], ["send-unit.py", "--warm"]):
        monkeypatch.setattr("sys.argv", argv)
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"session_id": "x"}'))
        send_unit.main()  # exits before reading anything


def test_hooks_register_the_stop_capture_and_codex_compaction(suite):
    require_coding(suite)
    hooks = json.loads(suite.hooks_json.read_text())["hooks"]
    stop = [h["command"] for group in hooks["Stop"] for h in group["hooks"]]
    assert any("send-unit.py" in c for c in stop)
    if suite.name == "codex":
        assert "compact" in hooks["SessionStart"][0]["matcher"].split("|")


# -- Capture -------------------------------------------------------------------------


def test_capture_sends_every_call_including_failures_and_reads(
    host_cm, tmp_path, repo, monkeypatch
):
    # Host-dependent: parse_transcript dispatches on agent().
    cm = host_cm
    sent = record_sent(cm, monkeypatch)
    path = _transcript(cm, tmp_path)
    request = "Fix it" if cm.agent() == "codex" else "Fix the parser"
    # The prompt hook runs a second after the transcript's prompt record.
    monkeypatch.setattr(cm, "now_iso", lambda: "2026-11-05T10:00:01+00:00")
    cm.begin_turn(SESSION_KEY, request, str(repo))
    head = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("x = 2\n")
    git(repo, "commit", "-qam", "second")
    end = git(repo, "rev-parse", "HEAD")

    cm.capture({"transcript_path": str(path), "cwd": str(repo), "last_assistant_message": "Done."})

    traces = [e for e in sent if e["type"] == "trace"]
    qa = [e for e in sent if e["type"] == "qa"]
    if cm.agent() == "codex":
        assert [t["origin_function"] for t in traces] == ["Bash", "Bash", "Edit"]
        assert [t["status"] for t in traces] == ["success", "error", "success"]
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


def test_subagent_calls_are_read_from_their_own_files(cm, sent, tmp_path, repo):
    path = claude_transcript(
        tmp_path / "s.jsonl", [("t1", "Agent", {"prompt": "look"}, "ok", False)]
    )
    sub = tmp_path / "s" / "subagents"
    sub.mkdir(parents=True)
    claude_transcript(
        sub / "agent-a1.jsonl", [("s1", "Grep", {"pattern": "def"}, "app.py", False)], answer="sub"
    )
    cm.capture({"transcript_path": str(path), "cwd": str(repo), "last_assistant_message": "Done."})
    names = [e["origin_function"] for e in sent if e["type"] == "trace"]
    assert names == ["Agent", "Grep"] or sorted(names) == ["Agent", "Grep"]


def test_secrets_and_private_spans_are_removed(cm, sent, tmp_path, repo):
    calls = [("t1", "Bash", {"command": "echo <private>hunter2</private>"}, "ok", False)]
    prompt = "token <private>s3cret</private> please"
    path = claude_transcript(tmp_path / "s.jsonl", calls, request=prompt)
    cm.begin_turn(SESSION_KEY, prompt, str(repo))
    cm.capture({"transcript_path": str(path), "cwd": str(repo), "last_assistant_message": "ok"})
    text = json.dumps(sent)
    assert "hunter2" not in text and "s3cret" not in text and "[private]" in text


def test_plugin_invocations_are_never_sent(cm, sent, tmp_path, repo):
    command = "bash ~/.claude/plugins/cognee/scripts/cognee-search.sh foo"
    path = claude_transcript(
        tmp_path / "s.jsonl",
        [
            ("t1", "Bash", {"command": command}, "x", False),
            ("t2", "Bash", {"command": "pwd"}, "/x", False),  # control: is sent
        ],
    )
    cm.capture({"transcript_path": str(path), "cwd": str(repo), "last_assistant_message": "ok"})
    traces = [e for e in sent if e["type"] == "trace"]
    assert [t["method_params"]["command"] for t in traces] == ["pwd"]


def test_capture_is_off_when_the_server_is_too_old(cm, sent, tmp_path, repo, monkeypatch):
    monkeypatch.setattr(cm, "server_supported", lambda: False)
    path = _transcript(cm, tmp_path)
    assert cm.capture({"transcript_path": str(path), "cwd": str(repo)}) == {
        "skipped": "server_unsupported"
    }
    assert sent == []


def test_cloud_mode_spools_and_local_mode_buffers(host_cm, suite, isolated_modules, monkeypatch):
    # Host-dependent: only Claude Code ships the cloud worker _cloud_spool reads.
    cm = host_cm
    common = isolated_modules(suite, "_plugin_common")
    buffered, spooled = [], []
    monkeypatch.setattr(common, "append_warmup_entry", lambda d, s, e: buffered.append(e))
    entries = [{"type": "trace", "origin_function": "Bash"}, {"type": "qa", "question": "q"}]
    monkeypatch.setattr(cm, "_cloud_spool", lambda: False)
    cm._deliver("agent_sessions", "claude_s1", entries)
    assert buffered == entries
    assert common.read_turn_count("claude_s1") == len(entries)  # once per entry
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

    def replying(spec):
        monkeypatch.setattr(common, "_json_http_request", lambda *a, **k: spec)

    replying(entry_spec())
    assert cm.check_server_support(refresh=True)["ok"] is True
    # Both schemas present, the times missing: a real old server.
    replying({"components": {"schemas": {"QAEntry": {"properties": {}}, "TraceEntry": {}}}})
    assert cm.check_server_support(refresh=True)["ok"] is False
    replying(entry_spec(qa=("question",)))
    assert cm.check_server_support(refresh=True)["ok"] is False
    replying(entry_spec(trace=()))
    assert cm.check_server_support(refresh=True)["ok"] is False

    def down(*a, **k):
        raise OSError("refused")

    monkeypatch.setattr(common, "_json_http_request", down)
    # A failed forced check says "could not tell"; the cached verdict stays for
    # the callers within its hour.
    result = cm.check_server_support(refresh=True)
    assert result["ok"] is None and "refused" in result["reason"]
    assert cm.check_server_support()["ok"] is False


# -- Selection -----------------------------------------------------------------------


def _newest_first(lessons):
    return sorted(
        lessons, key=lambda item: (item["session_date"], item["created_at"]), reverse=True
    )


def test_only_the_newest_baseline_and_the_two_newest_pitfalls_are_used(cm):
    baselines = [
        lesson(f"Tests: {n} pass.", kind="state", key="test-baseline", date=f"2026-11-0{n}")
        for n in (1, 3)
    ]
    pitfalls = [
        lesson(
            f"Pitfall {n} → use: x", kind="pitfall", key=f"pitfall:Bash:{n}", date=f"2026-11-0{n}"
        )
        for n in (1, 2, 3)
    ]
    chosen = cm.select_lessons(_newest_first(baselines + pitfalls), "")
    assert [item["body"] for item in chosen] == [
        "Tests: 3 pass.",
        "Pitfall 3 → use: x",
        "Pitfall 2 → use: x",
    ]


def test_recalled_lessons_follow_in_recall_order_and_duplicates_are_skipped(cm):
    lessons = _newest_first(
        [
            lesson("Parser lives in src/parse.py.", date="2026-11-02"),
            lesson("parser   LIVES in src/parse.py.", date="2026-11-01"),
            lesson("CLI entry is cli/main.py.", date="2026-11-03"),
        ]
    )
    recalled = ["cli entry is cli/main.py.", "Parser lives in src/parse.py."]
    chosen = [item["body"] for item in cm.select_lessons(lessons, recalled)]
    assert chosen == ["CLI entry is cli/main.py.", "Parser lives in src/parse.py."]


def test_lesson_documents_parse_headers_title_and_statement(cm):
    text = (
        "Lesson-Type: pitfall\nState-Key: pitfall:Edit:x:py\n"
        "# Session learning (session claude_1)\n\n"
        "Edit fails → use: a script. (seen in 2 sessions)\n"
    )
    assert cm.parse_lesson_text(text) == {
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
    assert cm.deliver(SESSION_KEY, "first", "/x") == "BLOCK"
    assert cm.deliver(SESSION_KEY, "second", "/x") == ""
    cm.reset_delivery(SESSION_KEY)
    assert cm.deliver(SESSION_KEY, "third", "/x") == "BLOCK"
    assert calls == ["first", "third"]


def test_a_failed_lookup_is_retried_on_the_next_prompt(cm, monkeypatch):
    def boom(q, cwd):
        raise TimeoutError("slow server")

    monkeypatch.setattr(cm, "build_block", boom)
    assert cm.deliver(SESSION_KEY, "first", "/x") == ""
    monkeypatch.setattr(cm, "build_block", lambda q, cwd: "BLOCK")
    assert cm.deliver(SESSION_KEY, "second", "/x") == "BLOCK"


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
                "git_commit": git(repo, "rev-parse", "HEAD"),
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
    git(repo, "commit", "-qam", "change app")

    block = cm.build_block("run the tests", str(repo))
    assert block.startswith(
        "Project memory (Cognee), learned from 1 earlier session in the agent_sessions dataset."
    )
    assert "- All 12 tests pass. (session 2026-11-03, commit " in block
    assert "src/app.py changed since)" in block
    assert "Other repo" not in block
    # The newest commit is listed by hash, date and files; subjects are never shown.
    newest = git(repo, "rev-parse", "--short", "HEAD")
    assert "Recent commits (newest first):" in block
    assert f"{newest} (" in block.split("Recent commits (newest first):", 1)[1]
    assert "change app" not in block
    assert len(block) < 2300


def test_no_lessons_means_no_block(cm, suite, isolated_modules, monkeypatch, repo):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "_json_http_request", lambda *a, **k: [])
    monkeypatch.setattr(common, "dataset_id_for", lambda name, *a: "ds-1")
    assert cm.build_block("anything", str(repo)) == ""


@pytest.mark.parametrize(
    "error",
    [
        OSError("connection refused"),
        urllib.error.HTTPError("/api/v1/recall", 404, "Not Found", None, None),
    ],
    ids=["down", "404"],
)
def test_server_down_or_404_recall_gives_no_recalled_lessons(
    cm, suite, isolated_modules, monkeypatch, error
):
    common = isolated_modules(suite, "_plugin_common")

    def down(*a, **k):
        raise error

    monkeypatch.setattr(common, "_json_http_request", down)
    assert cm.recall_passages("q", "agent_sessions", "ds-1", 10**12) == []


# -- Git tags ---------------------------------------------------------------------------


def test_tags_name_changed_and_deleted_files_case_insensitively(cm, repo):
    commit = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("changed\n")
    (repo / "src" / "old.py").unlink()
    git(repo, "commit", "-qam", "edit readme, drop old")
    lesson = {
        "git_commit": commit,
        "session_date": "2026-11-03",
        "files": ["readme.md", "old.py", "app.py", "never.py"],
    }
    tag = cm.lesson_tag(lesson, str(repo))
    short = git(repo, "rev-parse", "--short", commit)
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
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    commit = git(repo, "rev-parse", "HEAD")
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text("2\n")
    tag = cm.lesson_tag({"git_commit": commit, "files": ["a.py", "b.py", "c.py"]}, str(repo))
    assert tag.endswith("a.py, b.py +1 more changed since)")


def test_recent_commits_line_format(cm, repo):
    (repo / "src" / "app.py").write_text("x = 1\ny = 2\n")
    git(repo, "commit", "-qam", "grow")
    lines = cm.recent_commits(str(repo))
    assert len(lines) == 2
    assert lines[0].endswith("src/app.py (+1/−0 lines)")
    assert "(+3/−0 lines)" in lines[1] and "+1 more" in lines[1]


def test_outside_git_there_are_no_tags_and_no_commits(cm, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cm.git_root(str(plain)) == ""
    assert cm.recent_commits(str(plain)) == []
    block = cm.render_block(
        "ds", 2, [lesson("A fact.", commit="abc1234")], str(plain), in_git=False
    )
    assert (
        "- A fact." in block
        and "Recent commits" not in block
        and "commit" not in block.split("Lessons:")[1]
    )
