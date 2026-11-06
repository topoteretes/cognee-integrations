"""Coding memory mode: lesson recall, the block budget, fetch order, git tags and
the server support probe (SDK-1336 review fixes)."""

from __future__ import annotations

from pathlib import Path

import pytest
from utils.coding_memory import (
    entry_spec,
    git,
    lesson,
)


@pytest.fixture
def common(cm, suite, isolated_modules):
    return isolated_modules(suite, "_plugin_common")


@pytest.fixture
def rt(cm, suite, isolated_modules):
    return isolated_modules(suite, "_recall_text")


def _document(body, *, kind="other", key="", session="claude_old") -> str:
    head = f"Lesson-Type: {kind}\n" + (f"State-Key: {key}\n" if key else "")
    return f"{head}# Session learning (session {session})\n\n{body}\n"


def _graph_item(rt, passages, *, entities="", facts="") -> dict:
    text = rt.PASSAGES_HEADING + "\n" + rt.PASSAGE_SEPARATOR.join(passages)
    if entities:
        text += "\n\n" + rt.ENTITIES_HEADING + "\n" + entities
    if facts:
        text += "\n\n" + rt.FACTS_HEADING + "\n" + facts
    return {"source": "graph", "dataset_name": "agent_sessions", "text": text}


# -- Finding 4: recalled lessons are matched on the passages' plain text ----------------

QUOTED = 'Run "uv run pytest -q" from integrations/tests; the "-q" flag keeps the output short.'
BACKSLASH = r"Config lives in C:\Users\dev\cognee\.env on Windows runners, not in the repo root."
BROKEN = 'The parser fails on CRLF input.\nUse splitlines() rather than split("\\n") in it.'
ENTITY_ONLY = "Entity-only lesson: the graph names it, but no passage carries it."


def test_recalled_passages_with_quotes_backslashes_and_line_breaks_are_selected(cm, rt):
    lessons = [lesson(body) for body in (QUOTED, BACKSLASH, BROKEN, ENTITY_ONLY)]
    item = _graph_item(
        rt,
        [
            _document(BROKEN),
            "source: lesson-doc\n" + _document(QUOTED),
            _document(BACKSLASH),
        ],
        entities=f'- "{ENTITY_ONLY}" is a concept.\n- Parser belongs to set session_learnings.',
        facts=f"- {ENTITY_ONLY} --relates_to--> parser",
    )
    passages = cm.recalled_passages([item])
    assert len(passages) == 3
    chosen = [lesson["body"] for lesson in cm.select_lessons(lessons, passages)]
    # The server's passage order is the relevance order; an entity or fact line
    # quoting a lesson is not a recall of it.
    assert chosen == [BROKEN, QUOTED, BACKSLASH]


def test_recall_reads_the_full_prompt_reply_of_the_server(cm, rt, common, monkeypatch):
    context = _graph_item(rt, [_document(BACKSLASH), _document(QUOTED)])["text"]
    reply = [
        {
            "source": "graph",
            "text": "Answer from the context.\nThe question is: `fix the parser`\n"
            "Context:\n`" + context + "`",
        },
        {"source": "session", "text": rt.PASSAGES_HEADING + "\n" + _document(BROKEN)},
    ]
    seen = []

    def request(path, payload=None, **kwargs):
        seen.append((path, payload))
        return reply

    monkeypatch.setattr(common, "_json_http_request", request)
    passages = cm.recall_passages("fix the parser", "agent_sessions", "ds-1", 10**12)
    assert seen[0][0] == "/api/v1/recall" and seen[0][1]["dataset_ids"] == ["ds-1"]
    lessons = [lesson(QUOTED), lesson(BACKSLASH), lesson(BROKEN)]
    chosen = [lesson["body"] for lesson in cm.select_lessons(lessons, passages)]
    # The closing backtick of the template is not part of the last passage; the
    # session item is not graph memory.
    assert chosen == [BACKSLASH, QUOTED]


# -- Finding 5: one budget, measured on the rendered line --------------------------------


def _changed_repo(repo: Path) -> str:
    commit = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("x = 3\n")
    (repo / "src" / "old.py").unlink()
    git(repo, "commit", "-qam", "change app, drop old")
    return commit


def test_every_selected_lesson_is_rendered_and_the_lessons_fit_the_budget(cm, repo):
    commit = _changed_repo(repo)
    files = ["src/app.py", "src/old.py"]

    def body(n):
        return (f"Lesson {n}: " + "the build needs the generated stubs first. " * 8)[:290]

    lessons = [
        lesson(body(0), kind="state", key="test-baseline", commit=commit, files=files),
        lesson(body(1), kind="pitfall", key="pitfall:a", commit=commit, files=files),
        lesson(body(2), kind="pitfall", key="pitfall:b", commit=commit, files=files),
        lesson(body(3), commit=commit, files=files),
        lesson(body(4), commit=commit, files=files),
    ]
    recalled = [_document(body(3)), _document(body(4))]
    line_for = cm._line_maker(str(repo), True)
    tag = cm.lesson_tag(lessons[0], str(repo))
    assert 80 <= len(tag) <= 95 and "src/old.py no longer exists" in tag

    chosen = cm.select_lessons(lessons, recalled, line_for=line_for)
    block = cm.render_block("ds", 2, chosen, str(repo), True, line_for)
    section = block.split("Lessons:\n", 1)[1].split("\nRecent commits", 1)[0]
    assert len(chosen) == 3
    for item in chosen:
        assert f"- {item['body']} {tag}" in section
    assert len(section) + 1 <= cm.LESSON_BUDGET


def test_a_lesson_that_fits_only_without_its_tag_is_skipped(cm):
    def line_for(lesson):
        return f"- {lesson['body']} " + "T" * 100

    first = lesson("A" * 1000, kind="state", key="test-baseline")
    tight = lesson("B" * 300, kind="pitfall", key="pitfall:a", date="2026-11-03")
    shorter = lesson("C" * 200, kind="pitfall", key="pitfall:b", date="2026-11-02")
    # 1104 + 404 > 1500 with the tag (1407 without); 1104 + 304 fits.
    chosen = cm.select_lessons([first, tight, shorter], [], line_for=line_for)
    assert [lesson["body"][0] for lesson in chosen] == ["A", "C"]


def test_selection_measures_at_most_the_candidate_cap(cm):
    measured = []

    def line_for(lesson):
        measured.append(lesson["body"])
        return "- " + lesson["body"]

    lessons = [lesson(f"Fact {n}.") for n in range(20)]
    recalled = [_document(f"Fact {n}.") for n in range(20)]
    chosen = cm.select_lessons(lessons, recalled, line_for=line_for, max_candidates=4)
    assert [lesson["body"] for lesson in chosen] == ["Fact 0.", "Fact 1.", "Fact 2.", "Fact 3."]
    assert len(measured) == 4


def test_git_runs_ls_files_once_per_block(cm, repo, monkeypatch):
    commit = _changed_repo(repo)
    calls = []
    real = cm._git

    def counting(cwd, *args, **kwargs):
        calls.append(args[0])
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(cm, "_git", counting)
    lessons = [lesson(f"Fact {n}.", commit=commit, files=["src/app.py"]) for n in range(4)]
    line_for = cm._line_maker(str(repo), True)
    cm.select_lessons(lessons, [_document(f"Fact {n}.") for n in range(4)], line_for=line_for)
    assert calls.count("ls-files") == 1
    assert calls.count("ls-tree") == 1 and calls.count("diff") == 1


# -- Finding 12: the bodies the block needs are fetched first ----------------------------


def _cold_listing(count=40, baseline_at=39, pitfalls=(12, 20, 31)):
    rows, bodies = [], {}
    for index in range(count):
        data_id = f"22222222-0000-0000-0000-{index:012d}"
        if index == baseline_at:
            kind, key = "state", "test-baseline"
        elif index in pitfalls:
            kind, key = "pitfall", f"pitfall:Bash:{index}"
        else:
            kind, key = "location", ""
        rows.append(
            {
                "id": data_id,
                "createdAt": f"2026-10-01T10:{59 - index:02d}:00Z",
                "externalMetadata": {
                    "lesson_type": kind,
                    "state_key": key,
                    "session_id": f"claude_{index}",
                },
            }
        )
        bodies[data_id] = _document(f"Lesson number {index}.", kind=kind, key=key)
    return rows, bodies


def test_a_cold_cache_fetches_the_baseline_and_pitfalls_first(cm, common, monkeypatch):
    rows, bodies = _cold_listing()
    fetched = []

    def request(path, payload=None, **kwargs):
        if path.startswith("/api/v1/datasets/ds-1/data"):
            return rows
        raise AssertionError(path)

    def raw(dataset_id, data_id, timeout):
        fetched.append(data_id)
        return bodies[data_id]

    monkeypatch.setattr(common, "_json_http_request", request)
    monkeypatch.setattr(common, "dataset_id_for", lambda name, *a: "ds-1")
    monkeypatch.setattr(cm, "_raw_text", raw)
    _, lessons = cm.list_lessons("agent_sessions", [], 10**12, max_fetches=3)
    ids = [row["id"] for row in rows]
    assert fetched == [ids[39], ids[12], ids[20]]
    block = cm.render_block("ds", 3, cm.select_lessons(lessons, []), "", False)
    assert "- Lesson number 39." in block
    assert "- Lesson number 12." in block and "- Lesson number 20." in block

    # Warming downloads in the same order (the three above are cached now).
    for path in cm._cache_dir().glob("*.txt"):
        path.unlink()
    fetched.clear()
    monkeypatch.setattr(cm, "project_scope", lambda *a: [])
    assert cm.warm_lessons("/x") == 40
    rest = [ids[i] for i in range(40) if i not in (39, 12, 20, 31)]
    assert fetched == [ids[39], ids[12], ids[20], ids[31], *rest]


def test_the_returned_lessons_keep_the_newest_first_order(cm, common, monkeypatch):
    rows, bodies = _cold_listing(count=6, baseline_at=5, pitfalls=(2,))
    monkeypatch.setattr(
        common, "_json_http_request", lambda path, *a, **k: rows if "/data" in path else []
    )
    monkeypatch.setattr(common, "dataset_id_for", lambda name, *a: "ds-1")
    monkeypatch.setattr(cm, "_raw_text", lambda ds, data_id, timeout: bodies[data_id])
    _, lessons = cm.list_lessons("agent_sessions", [], 10**12)
    assert [lesson["body"] for lesson in lessons] == [f"Lesson number {n}." for n in range(6)]


# -- Finding 13: leading dots in file names --------------------------------------------


def test_dotfiles_and_dot_directories_keep_their_leading_dot(cm, repo):
    (repo / ".env.template").write_text("A=1\n")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "dotfiles")
    commit = git(repo, "rev-parse", "HEAD")
    (repo / ".env.template").write_text("A=2\n")
    (repo / "src" / "app.py").write_text("x = 9\n")
    (repo / ".github" / "workflows" / "ci.yml").unlink()
    git(repo, "commit", "-qam", "edit")

    assert cm._match(".env.template", [".env.template", "env.template"]) == [".env.template"]
    assert cm._match("./src/app.py", ["src/app.py"]) == ["src/app.py"]
    tagged = {
        "git_commit": commit,
        "files": [".env.template", ".github/workflows/ci.yml", "./src/app.py"],
    }
    tag = cm.lesson_tag(tagged, str(repo))
    assert tag.endswith(
        "; .env.template, ./src/app.py changed since; .github/workflows/ci.yml no longer exists)"
    )


# -- Finding 10: the support probe says False only on a real old spec -------------------


def _cached(cm, common):
    url = common._normalize_service_url(common._local_api_url())
    return cm._read_json(cm._support_file()).get(url)


@pytest.mark.parametrize(
    "reply",
    [
        None,
        {"components": {}},
        {"components": {"schemas": {"TraceEntry": {"properties": {"time": {}}}}}},
    ],
    ids=["empty", "no-schemas", "no-QAEntry"],
)
def test_a_reply_without_the_entry_schemas_is_unknown_and_keeps_the_cache(
    cm, common, monkeypatch, reply
):
    monkeypatch.setattr(common, "_json_http_request", lambda *a, **k: entry_spec())
    assert cm.check_server_support(refresh=True)["ok"] is True
    monkeypatch.setattr(common, "_json_http_request", lambda *a, **k: reply)
    result = cm.check_server_support(refresh=True)
    assert result["ok"] is None and result["reason"].startswith("could not tell")
    assert _cached(cm, common)["ok"] is True


# -- The repository root, not the hook's directory ---------------------------------------------


def test_a_block_built_in_a_subdirectory_tags_lessons_as_from_the_root(cm, repo, monkeypatch):
    sub = repo / "pkg" / "sub"
    sub.mkdir(parents=True)
    (sub / "a.py").write_text("a = 1\n")
    (repo / "top.py").write_text("t = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add files")
    commit = git(repo, "rev-parse", "HEAD")
    (sub / "a.py").write_text("a = 2\n")
    (repo / "top.py").unlink()
    git(repo, "commit", "-qam", "change a, drop top")
    files = ["pkg/sub/a.py", "top.py"]
    lessons = [
        lesson(
            "Run the suite first.", kind="state", key="test-baseline", commit=commit, files=files
        )
    ]
    monkeypatch.setattr(cm, "project_scope", lambda *a: [])
    monkeypatch.setattr(cm, "list_lessons", lambda *a, **k: ("ds-1", [dict(x) for x in lessons]))
    monkeypatch.setattr(cm, "recall_passages", lambda *a, **k: [])

    from_root = cm.build_block("q", str(repo))
    from_sub = cm.build_block("q", str(sub))
    assert "pkg/sub/a.py changed since" in from_root and "top.py no longer exists" in from_root
    assert from_sub == from_root
