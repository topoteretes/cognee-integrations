"""Symbol stamps: the symbols a turn's edits changed, and lessons marked by them.

At the end of a turn the edit calls' line ranges go through the server's
``locate`` CODE operation and the returned symbols ride in the turn's context;
in the lessons block one ``locate`` call compares the stamps of the lessons
shown with the code graph. The server is a fake opener answering the contract
of cognee 029d36a78. ``_symbol_stamps`` ships with Claude Code only; Codex has
the same ``_coding_memory`` and no stamps.
"""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest
from utils.code_analysis import RECALL, FakeServer, code_rows, index_by_path
from utils.coding_memory import SESSION_KEY as KEY
from utils.coding_memory import git, lesson, said, tool, user, write_records

LINE_TOTAL = {
    "repo": "repo",
    "file": "src/app.py",
    "name": "src/app.line_total",
    "kind": "function",
    "body_hash": "aaaa1111",
}
HELPER = {**LINE_TOTAL, "name": "src/app.Store.helper", "kind": "method", "body_hash": "bbbb2222"}


def located(*symbols, missing=()) -> tuple:
    found = [{**s, "line": 1, "end_line": 2} for s in symbols]
    return 200, code_rows(
        {"operation": "locate", "symbols": found, "unmatched": [], "missing": list(missing)}
    )


@pytest.fixture
def env(cm, repo, monkeypatch):
    """Coding memory with the stamps module bound to a fake server, and the
    test repository indexed by path with a current fingerprint."""
    stamps = cm._stamps()
    assert stamps is not None
    server = FakeServer()
    monkeypatch.setattr(stamps._code_analysis, "_HTTPS_OPENER", SimpleNamespace(open=server))
    monkeypatch.setattr(stamps, "_runtime", lambda: server.rt)
    logged: list = []
    monkeypatch.setattr(
        stamps, "hook_log", lambda event, detail=None: logged.append((event, detail))
    )
    reindexed: list = []

    def do_index_repo(service_url, api_key, spec, dataset, **kwargs):
        reindexed.append(kwargs)
        return {"ok": True, "repository": spec}

    monkeypatch.setattr(stamps._code_graph, "do_index_repo", do_index_repo)
    (repo / "src" / "app.py").write_text(
        "def line_total(price, qty):\n    return price * qty\n\n\nclass Store:\n"
        "    def helper(self):\n        return 1\n"
    )
    git(repo, "commit", "-qam", "app")
    state = index_by_path(stamps._code_graph, repo)
    return SimpleNamespace(
        cm=cm,
        stamps=stamps,
        server=server,
        repo=repo,
        root=state["repo_root"],
        logged=logged,
        reindexed=reindexed,
    )


def edit(path, new: str, name="Edit", **extra) -> dict:
    return {"name": name, "input": {"file_path": str(path), "new_string": new, **extra}}


# -- Line ranges from the edit calls' own inputs -------------------------------------


def test_an_edit_is_located_where_its_new_text_sits_now(env):
    app = env.repo / "src" / "app.py"
    calls = [
        edit(app, "    def helper(self):\n        return 1\n"),
        edit(app, "return price * qty"),
        {"name": "Write", "input": {"file_path": str(env.repo / "src" / "old.py"), "content": "y"}},
        {
            "name": "MultiEdit",
            "input": {"file_path": "README.md", "edits": [{"new_string": "hello"}]},
        },
    ]
    real = os.path.realpath
    assert env.stamps.edit_locations(calls, env.root) == [
        {"file": real(app), "start": 6, "end": 7},
        {"file": real(app), "start": 2, "end": 2},
        {"file": real(env.repo / "src" / "old.py"), "start": 1, "end": 1},
        {"file": real(env.repo / "README.md"), "start": 1, "end": 1},
    ]


def test_edits_that_leave_no_range(env, tmp_path):
    app = env.repo / "src" / "app.py"
    outside = tmp_path / "elsewhere.py"
    outside.write_text("z = 1\n")
    calls = [
        edit(app, ""),  # a deletion
        edit(app, "text a later edit replaced"),
        {**edit(app, "return price * qty"), "is_error": True},
        edit(outside, "z = 1"),
        edit(env.repo / "gone.py", "x"),
        {"name": "Bash", "input": {"command": "sed -i s/a/b/ src/app.py"}},
        {"name": "Read", "input": {"file_path": str(app)}},
    ]
    assert env.stamps.edit_locations(calls, env.root) == []


def test_replace_all_covers_every_occurrence(env):
    path = env.repo / "src" / "old.py"
    path.write_text("a = 1\nb = 2\na = 1\n")
    assert [
        (r["start"], r["end"])
        for r in env.stamps.edit_locations([edit(path, "a = 1", replace_all=True)], env.root)
    ] == [(1, 1), (3, 3)]


# -- Stamping a turn -----------------------------------------------------------------


def test_stamps_are_the_located_symbols_cut_to_the_five_keys(env):
    env.server.on("POST", RECALL, located(LINE_TOTAL, {**HELPER, "body_hash": None}))
    out = env.stamps.stamp_turns(
        {"u1": [edit(env.repo / "src" / "app.py", "return price * qty")], "u2": []}, env.root
    )
    assert out == {"u1": [LINE_TOTAL, {**HELPER, "body_hash": None}]}
    body = env.server.bodies(RECALL)[0]
    assert body["code_query"] == {
        "operation": "locate",
        "locations": [
            {"file": os.path.realpath(env.repo / "src" / "app.py"), "start": 2, "end": 2}
        ],
    }
    assert body["datasets"] == ["codebase-proj-1234abcd"] and body["scope"] == ["code"]
    # The tree matches the indexed fingerprint: no re-index.
    assert env.reindexed == []


def test_a_malformed_symbol_is_left_out_not_sent(env):
    env.server.on(
        "POST", RECALL, located(LINE_TOTAL, {**HELPER, "kind": ""}, {**HELPER, "body_hash": 7})
    )
    out = env.stamps.stamp_turns(
        {"u": [edit(env.repo / "src" / "app.py", "price * qty")]}, env.root
    )
    assert out == {"u": [LINE_TOTAL]}


def test_a_changed_tree_is_reindexed_and_waited_for_before_locate(env):
    (env.repo / "src" / "app.py").write_text("def line_total(price, qty):\n    return 0\n")
    env.server.on("POST", RECALL, located(LINE_TOTAL))
    out = env.stamps.stamp_turns({"u": [edit(env.repo / "src" / "app.py", "return 0")]}, env.root)
    assert out == {"u": [LINE_TOTAL]}
    assert len(env.reindexed) == 1 and env.reindexed[0]["run_in_background"] is False
    assert env.reindexed[0]["timeout"] < env.stamps.STAMP_BUDGET_SECONDS
    # The fingerprint advanced: the next turn does not index again.
    env.stamps.stamp_turns({"u": [edit(env.repo / "src" / "app.py", "return 0")]}, env.root)
    assert len(env.reindexed) == 1


def test_a_graph_that_cannot_be_refreshed_gives_no_stamps(env, monkeypatch):
    (env.repo / "src" / "app.py").write_text("x = 3\n")
    monkeypatch.setattr(
        env.stamps._code_graph,
        "do_index_repo",
        lambda *a, **k: {"error": "HTTP 403", "status": 403},
    )
    assert (
        env.stamps.stamp_turns({"u": [edit(env.repo / "src" / "app.py", "x = 3")]}, env.root) == {}
    )
    assert env.server.calls == []
    assert env.logged == [("symbol_stamps_skipped", {"reason": "graph_not_current"})]


def test_a_turn_without_edits_asks_the_server_nothing(env):
    calls = [{"name": "Read", "input": {"file_path": str(env.repo / "src" / "app.py")}}]
    assert env.stamps.stamp_turns({"u": calls}, env.root) == {}
    assert env.server.calls == [] and env.logged == []


def test_a_repository_that_is_not_indexed_gives_no_stamps(env):
    for path in env.stamps._code_graph._STATE_DIR.glob("*.json"):
        path.unlink()
    assert env.stamps.stamp_turns({"u": [edit(env.repo / "src" / "app.py", "qty")]}, env.root) == {}
    assert env.server.calls == []
    assert [event for event, _ in env.logged] == ["symbol_stamps_skipped"]


def test_a_server_without_locate_is_asked_once_an_hour(env):
    env.server.on(
        "POST", RECALL, (422, {"detail": "Unsupported CODE operation", "code": "unknown_operation"})
    )
    groups = {"u": [edit(env.repo / "src" / "app.py", "price * qty")]}
    assert env.stamps.stamp_turns(groups, env.root) == {}
    assert env.stamps.stamp_turns(groups, env.root) == {}
    assert len(env.server.calls) == 1
    assert [detail["reason"] for _, detail in env.logged] == [
        "unknown_operation",
        "no_graph_or_operation",
    ]


def test_a_slow_locate_is_cut_at_its_deadline(env):
    def slow(body):
        time.sleep(1.0)
        return located(LINE_TOTAL)

    env.server.on("POST", RECALL, slow)
    started = time.monotonic()
    out = env.stamps.turn_symbols([edit(env.repo / "src" / "app.py", "price * qty")], env.root, 0.1)
    assert out == [] and time.monotonic() - started < 0.8
    assert env.logged == [("symbol_stamps_skipped", {"reason": "timeout", "locations": 1})]


def test_the_stamp_budget_stops_before_the_next_turn(env):
    env.server.on("POST", RECALL, located(LINE_TOTAL))
    groups = {key: [edit(env.repo / "src" / "app.py", "price * qty")] for key in ("a", "b")}
    assert env.stamps.stamp_turns(groups, env.root, budget=0.0) == {}
    assert env.logged[-1] == ("symbol_stamps_skipped", {"reason": "deadline", "turns": 2})


# -- The turn's context ----------------------------------------------------------------


@pytest.fixture
def transcript(env, tmp_path, monkeypatch):
    """Two requests; the first edited src/app.py, the second only read it."""
    monkeypatch.setattr(env.cm, "now_iso", lambda: "2026-11-05T10:00:01+00:00")
    env.cm.begin_turn(KEY, "Fix the total", str(env.repo))
    app = str(env.repo / "src" / "app.py")
    records = [
        user("ua", "Fix the total", "2026-11-05T10:00:00.000Z"),
        *tool(
            "t1",
            "c1",
            "Edit",
            {"file_path": app, "new_string": "return price * qty"},
            "ok",
            "2026-11-05T10:00:10.000Z",
        ),
        said("aa", "Fixed.", "2026-11-05T10:01:00.000Z"),
        user("ub", "Now explain it", "2026-11-05T10:05:00.000Z"),
        *tool("t2", "c2", "Read", {"file_path": app}, "text", "2026-11-05T10:05:10.000Z"),
        said("ab", "It multiplies.", "2026-11-05T10:06:00.000Z"),
    ]
    path = write_records(tmp_path / "s.jsonl", records)
    return {
        "transcript_path": str(path),
        "cwd": str(env.repo),
        "last_assistant_message": "It multiplies.",
    }


def contexts(sent) -> list:
    return [json.loads(e["context"]) for e in sent if e["type"] == "qa"]


def test_the_turn_that_edited_carries_its_symbols(env, sent, transcript):
    env.server.on("POST", RECALL, located(LINE_TOTAL))
    env.cm.capture(transcript)
    first, second = contexts(sent)
    assert first["symbols"] == [LINE_TOTAL]
    assert "symbols" not in second
    assert len(env.server.bodies(RECALL)) == 1


def test_any_failure_sends_the_turn_without_stamps(env, sent, transcript):
    env.server.on("POST", RECALL, (500, {"detail": "boom"}))
    summary = env.cm.capture(transcript)
    assert summary["qa"] == 2
    assert all("symbols" not in context for context in contexts(sent))
    assert [(event, detail["reason"]) for event, detail in env.logged] == [
        ("symbol_stamps_skipped", "http_500")
    ]


def test_a_request_already_sent_is_not_stamped_again(env, sent, transcript):
    env.server.on("POST", RECALL, located(LINE_TOTAL))
    env.cm.capture(transcript)
    env.server.calls.clear()
    env.cm.capture(transcript)
    assert env.server.calls == []


def test_a_plugin_without_the_stamps_module_sends_turns_as_before(
    env, sent, transcript, monkeypatch
):
    monkeypatch.setattr(env.cm, "_stamps", lambda: None)
    env.cm.capture(transcript)
    assert all("symbols" not in context for context in contexts(sent))
    assert env.server.calls == []


# -- The lessons block -----------------------------------------------------------------


def stamped(body, *symbols, commit, files=("src/app.py",)) -> dict:
    return {**lesson(body, commit=commit, files=files), "symbols": list(symbols)}


def test_a_lesson_is_marked_by_its_stamped_symbols(env):
    commit = git(env.repo, "rev-parse", "HEAD")
    (env.repo / "src" / "app.py").write_text("changed = True\n")
    git(env.repo, "commit", "-qam", "change app")
    lessons = [
        stamped("line_total multiplies.", LINE_TOTAL, HELPER, commit=commit),
        stamped("No stamps on this one.", commit=commit),
    ]
    gone = {k: HELPER[k] for k in ("repo", "file", "name", "kind")}
    env.server.on("POST", RECALL, located({**LINE_TOTAL, "body_hash": "cccc3333"}, missing=[gone]))
    assert env.stamps.mark_changed(lessons, env.root) is True
    assert lessons[0]["symbol_state"] == {"changed": ["line_total"], "gone": ["Store.helper"]}
    assert "symbol_state" not in lessons[1]
    short = git(env.repo, "rev-parse", "--short", commit)
    assert env.cm.lesson_line(lessons[0], env.root, True) == (
        f"- line_total multiplies. (session 2026-11-01, commit {short}; "
        "line_total changed since; Store.helper no longer exists)"
    )
    # A lesson without stamps keeps the file-level marker.
    assert env.cm.lesson_line(lessons[1], env.root, True).endswith("src/app.py changed since)")
    # One call, with the identity of every stamp and no hashes.
    (body,) = env.server.bodies(RECALL)
    assert body["code_query"] == {
        "operation": "locate",
        "symbols": [
            {k: s[k] for k in ("repo", "file", "name", "kind")} for s in (LINE_TOTAL, HELPER)
        ],
    }


def test_lessons_are_compared_with_a_graph_that_holds_the_working_tree(env, monkeypatch):
    """A tree edited since the last index is loaded before the stamps are compared;
    a graph that cannot be brought up to date marks nothing."""
    commit = git(env.repo, "rev-parse", "HEAD")
    (env.repo / "src" / "app.py").write_text("def line_total(price, qty):\n    return 1\n")
    env.server.on("POST", RECALL, located({**LINE_TOTAL, "body_hash": "cccc3333"}))
    lessons = [stamped("line_total multiplies.", LINE_TOTAL, commit=commit)]
    assert env.stamps.mark_changed(lessons, env.root) is True
    assert len(env.reindexed) == 1 and env.reindexed[0]["run_in_background"] is False
    assert lessons[0]["symbol_state"] == {"changed": ["line_total"], "gone": []}

    (env.repo / "src" / "app.py").write_text("def line_total(price, qty):\n    return 2\n")
    monkeypatch.setattr(
        env.stamps._code_graph,
        "do_index_repo",
        lambda *a, **k: {"error": "HTTP 403", "status": 403},
    )
    stale = [stamped("line_total multiplies.", LINE_TOTAL, commit=commit)]
    env.logged.clear()
    assert env.stamps.mark_changed(stale, env.root) is False
    assert "symbol_state" not in stale[0]
    assert env.logged == [("symbol_marks_skipped", {"reason": "graph_not_current", "stamps": 1})]


def test_an_unchanged_stamped_symbol_leaves_the_lesson_unmarked(env):
    commit = git(env.repo, "rev-parse", "HEAD")
    (env.repo / "src" / "app.py").write_text("# a comment elsewhere in the file\n")
    git(env.repo, "commit", "-qam", "touch app")
    lessons = [stamped("line_total multiplies.", LINE_TOTAL, commit=commit)]
    env.server.on("POST", RECALL, located(LINE_TOTAL))
    assert env.stamps.mark_changed(lessons, env.root)
    line = env.cm.lesson_line(lessons[0], env.root, True)
    assert "changed since" not in line and "no longer exists" not in line


def test_a_null_hash_on_either_side_cannot_tell(env):
    lessons = [
        {"symbols": [{**LINE_TOTAL, "body_hash": None}]},
        {"symbols": [HELPER]},
    ]
    env.server.on(
        "POST", RECALL, located({**LINE_TOTAL, "body_hash": "x"}, {**HELPER, "body_hash": None})
    )
    assert env.stamps.mark_changed(lessons, env.root)
    assert [item["symbol_state"] for item in lessons] == [{"changed": [], "gone": []}] * 2


@pytest.mark.parametrize(
    "answer,reason",
    [
        ((500, {"detail": "boom"}), "http_500"),
        ((422, {"detail": "no", "code": "unknown_operation"}), "unknown_operation"),
        (
            (200, [{"raw": {"operation": "locate", "symbols": [], "missing": []}}]),
            "nothing_matched",
        ),
        (ConnectionRefusedError("refused"), "unreachable"),
    ],
)
def test_a_failed_comparison_keeps_the_file_level_marker(env, answer, reason):
    commit = git(env.repo, "rev-parse", "HEAD")
    (env.repo / "src" / "app.py").write_text("changed = True\n")
    git(env.repo, "commit", "-qam", "change app")
    lessons = [stamped("line_total multiplies.", LINE_TOTAL, commit=commit)]
    env.server.on("POST", RECALL, answer)
    assert env.stamps.mark_changed(lessons, env.root) is False
    assert "symbol_state" not in lessons[0]
    assert env.cm.lesson_line(lessons[0], env.root, True).endswith("src/app.py changed since)")
    assert [(event, detail["reason"]) for event, detail in env.logged] == [
        ("symbol_marks_skipped", reason)
    ]


def test_lessons_without_stamps_cost_no_request(env):
    assert (
        env.stamps.mark_changed([lesson("plain"), {"symbols": [{"repo": "r"}]}], env.root) is False
    )
    assert env.server.calls == [] and env.logged == []


def test_the_block_marks_the_lessons_it_shows_with_one_call(env, monkeypatch):
    commit = git(env.repo, "rev-parse", "HEAD")
    (env.repo / "src" / "app.py").write_text("changed = True\n")
    git(env.repo, "commit", "-qam", "change app")
    lessons = [
        {
            **stamped("Run the suite first.", LINE_TOTAL, commit=commit),
            "state_key": "test-baseline",
        },
        {**stamped("Edits fail twice.", HELPER, commit=commit), "lesson_type": "pitfall"},
    ]
    monkeypatch.setattr(env.cm, "project_scope", lambda *a: [])
    monkeypatch.setattr(
        env.cm, "list_lessons", lambda *a, **k: ("ds-1", [dict(x) for x in lessons])
    )
    monkeypatch.setattr(env.cm, "recall_passages", lambda *a, **k: [])
    env.server.on("POST", RECALL, located(LINE_TOTAL, {**HELPER, "body_hash": "dddd4444"}))
    block = env.cm.build_block("q", str(env.repo))
    assert "- Run the suite first. (session 2026-11-01, commit" in block
    assert "Store.helper changed since" in block
    assert "src/app.py changed since" not in block
    assert len(env.server.bodies(RECALL)) == 1

    env.server.on("POST", RECALL, (500, {"detail": "boom"}))
    fallback = env.cm.build_block("q", str(env.repo))
    assert fallback.count("src/app.py changed since") == 2


def test_the_listing_reads_the_lessons_stamps(env, monkeypatch):
    rows = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "externalMetadata": {
                "lesson_type": "change",
                "files": ["src/app.py"],
                "symbols": [LINE_TOTAL, "junk"],
            },
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "externalMetadata": {"lesson_type": "change"},
        },
    ]
    import _plugin_common

    monkeypatch.setattr(_plugin_common, "_json_http_request", lambda *a, **k: rows)
    monkeypatch.setattr(env.cm, "resolve_dataset_id", lambda *a: "ds-1")
    monkeypatch.setattr(
        env.cm, "_cached_body", lambda data_id: "# Session learning (session s)\nBody."
    )
    _, lessons = env.cm.list_lessons("agent_sessions", [], time.monotonic() + 5)
    assert [item["symbols"] for item in lessons] == [[LINE_TOTAL], []]
