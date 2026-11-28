"""Coding memory while the agent works (Claude Code): lessons at session start
and with every prompt, per-prompt recall, file context on Read.

Coding mode keeps its one capture per request (``send-unit.py``) and reads
again during the session:

* SessionStart lists up to five of the project's lessons from the lesson cache
  (no query: the newest baseline, two pitfalls, then the newest lessons); with a
  cold cache there is no list and the first prompt's block covers it;
* a prompt that gets no lessons block is recalled for as outside coding mode,
  plus a "Lessons for this prompt" list: the lessons the server's lesson recall
  returned that also share a word with the prompt, at most five. A lesson the
  session already holds in full is one "shown earlier" line;
* lesson documents leave the recall section (the list shows them);
* ``file-context.py`` answers a Read; per-call capture and the pre-compact
  anchor stay off.

The server is faked at ``_coding_memory.list_lessons`` / ``recall_passages`` and
at ``recall_via_http`` (``utils.recall.drive_recall``).
"""

from __future__ import annotations

import io
import json
import sys
import types

import pytest
from utils.coding_memory import git, lesson
from utils.recall import SESSION_KEY, drive_recall, load_lookup


def _claude_only(suite) -> None:
    if suite.name != "claude-code":
        pytest.skip("the lessons lists and the coding-mode recall ship with Claude Code")


@pytest.fixture
def cl(suite, isolated_modules, monkeypatch, tmp_path):
    """``_coding_lessons`` in coding mode over a faked lesson store."""
    _claude_only(suite)
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    module = isolated_modules(suite, "_coding_lessons")
    cm = module._coding_memory
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(cm, "_session", lambda: ("claude_s1", "agent_sessions"))
    monkeypatch.setattr(cm, "server_supported", lambda: True)
    monkeypatch.setattr(cm, "project_scope", lambda *a: [])
    monkeypatch.setattr(cm, "_stamps", lambda: None)
    module.cwd = str(tmp_path)
    return module


def store(cl, monkeypatch, lessons: list, recalled=None) -> list:
    """Serve ``lessons`` (newest first) and a recall that returns ``recalled``
    (default: every lesson, in order). Returns the ``max_fetches`` each listing asked for."""
    asked: list = []

    def list_lessons(dataset, project, deadline, **kwargs):
        asked.append(kwargs.get("max_fetches"))
        return "dataset-id", [dict(item) for item in lessons]

    bodies = [item["body"] for item in lessons] if recalled is None else recalled
    monkeypatch.setattr(cl._coding_memory, "list_lessons", list_lessons)
    monkeypatch.setattr(cl._coding_memory, "recall_passages", lambda *a: list(bodies))
    return asked


def pricing(n: int, **fields) -> dict:
    return lesson(f"Pricing rule {n}: totals are rounded.", date=f"2026-11-{n:02d}", **fields) | {
        "data_id": f"id-{n}"
    }


def deadline() -> float:
    import time

    return time.monotonic() + 5.0


# -- Relevance and ranking -------------------------------------------------------------


def test_a_lesson_is_relevant_when_it_shares_a_word_with_the_prompt(cl):
    rounding = lesson(
        "inventory/pricing.py rounds compute_total to two decimals.", key="footprint:rounding"
    )
    for prompt in (
        "why is the pricing total off?",  # a word of the statement
        "what does compute_total return",  # an identifier, split on the underscore
        "numbers look rounded",  # rounded / rounds share five letters
        "footprint of this module",  # the state key
    ):
        assert cl.shares_a_term(cl.terms(prompt), rounding), prompt
    for prompt in ("how do I configure the kubernetes ingress for staging?", "the and for with"):
        assert not cl.shares_a_term(cl.terms(prompt), rounding), prompt
    stamped = dict(rounding, body="Keep it.", symbols=[{"name": "billing/Invoice.settle"}])
    assert cl.shares_a_term(cl.terms("who calls settle"), stamped)


def test_the_list_is_the_blocks_ranking_cut_to_five(cl):
    """Baseline, the two newest pitfalls, then recall order: ``select_lessons``."""
    baseline = lesson("Tests: 12 pass, pricing included.", kind="state", key="test-baseline")
    pitfalls = [
        lesson(
            f"Pricing pitfall {n} → use: x",
            kind="pitfall",
            key=f"pitfall:{n}",
            date=f"2026-11-0{n}",
        )
        for n in (3, 2, 1)
    ]
    others = [pricing(n) for n in (9, 8, 7, 6)]
    lessons = [*others, *pitfalls, baseline]
    recalled = [item["body"] for item in reversed(lessons)]
    chosen = [item["body"] for item in cl.relevant(lessons, recalled, "pricing")]
    assert chosen == [
        "Tests: 12 pass, pricing included.",
        "Pricing pitfall 3 → use: x",
        "Pricing pitfall 2 → use: x",
        "Pricing pitfall 1 → use: x",  # from here on, the recall's order
        "Pricing rule 6: totals are rounded.",
    ]


def test_a_lesson_must_be_recalled_and_share_a_word(cl):
    lessons = [
        pricing(3),
        pricing(2),
        lesson("The CLI entry point is cli/main.py.", date="2026-11-01"),
    ]
    # Not recalled: left out although it shares "pricing".
    recalled = [lessons[0]["body"], lessons[2]["body"]]
    assert [i["body"] for i in cl.relevant(lessons, recalled, "pricing totals")] == [
        "Pricing rule 3: totals are rounded."
    ]
    # Recalled (the server returns its nearest lessons whatever the distance)
    # but nothing in common with the prompt: no list.
    assert cl.relevant(lessons, [i["body"] for i in lessons], "kubernetes ingress") == []


# -- The prompt's list -----------------------------------------------------------------


def test_new_lessons_are_full_lines_and_known_ones_a_reference(cl, monkeypatch):
    long = pricing(
        4,
    )
    long["body"] = (
        "Pricing totals are stored in cents downstream, " + "so " * 60 + "round them. Then export."
    )
    lessons = [long, pricing(3), pricing(2)]
    store(cl, monkeypatch, lessons)
    first = cl.for_prompt(SESSION_KEY, "pricing totals", cl.cwd, deadline())
    assert [item["full"] for item in first] == [True, True, True]
    assert first[1]["line"] == "- Pricing rule 3: totals are rounded."

    cl.mark_shown(SESSION_KEY, [first[0]["lesson"], first[1]["lesson"]])
    again = cl.for_prompt(SESSION_KEY, "pricing totals", cl.cwd, deadline())
    assert [item["full"] for item in again] == [False, False, True]
    assert again[1]["line"] == "- Pricing rule 3: totals are rounded. (shown earlier)"
    # The first sentence, cut to REFERENCE_CHARS.
    reference = again[0]["line"]
    assert reference.startswith("- Pricing totals are stored in cents downstream, so so")
    assert reference.endswith("… (shown earlier)")
    assert len(reference) == len("- ") + cl.REFERENCE_CHARS + len(" (shown earlier)")


def test_a_lesson_of_the_first_prompt_block_counts_as_shown(cl, monkeypatch):
    store(cl, monkeypatch, [pricing(3), pricing(2)])
    block = (
        "Project memory (Cognee), learned from 2 earlier sessions.\nLessons:\n"
        "- Pricing rule 3: totals are rounded. (session 2026-11-03, commit 1a2b3c4)\n"
        "Recent commits (newest first):\n- 5d6e7f8 (2026-11-04): src/app.py (+4/−1 lines)"
    )
    cl.mark_shown(SESSION_KEY, block=block)
    items = cl.for_prompt(SESSION_KEY, "pricing", cl.cwd, deadline())
    assert [(item["lesson"]["data_id"], item["full"]) for item in items] == [
        ("id-3", False),
        ("id-2", True),
    ]


def test_a_prompt_with_no_relevant_lesson_gets_no_list(cl, monkeypatch):
    store(cl, monkeypatch, [pricing(3), pricing(2)])
    assert cl.for_prompt(SESSION_KEY, "configure the kubernetes ingress", cl.cwd, deadline()) == []
    assert cl.for_prompt(SESSION_KEY, "   ", cl.cwd, deadline()) == []
    store(cl, monkeypatch, [])
    assert cl.for_prompt(SESSION_KEY, "pricing", cl.cwd, deadline()) == []


def test_a_failing_server_gives_no_list(cl, monkeypatch):
    def down(*a, **k):
        raise OSError("refused")

    monkeypatch.setattr(cl._coding_memory, "list_lessons", down)
    assert cl.for_prompt(SESSION_KEY, "pricing", cl.cwd, deadline()) == []
    assert cl.at_session_start(SESSION_KEY, cl.cwd) == {}
    monkeypatch.setattr(cl._coding_memory, "server_supported", lambda: False)
    assert cl.at_session_start(SESSION_KEY, cl.cwd) == {}


def test_only_lesson_documents_are_lesson_passages(cl):
    document = (
        "[dataset agent_sessions]\nsource: text_1 (2026-11-26)\nLesson-Type: state\n"
        "State-Key: footprint:x\n# Session learning (session claude_1)\n\nRound totals."
    )
    assert cl.is_lesson_passage(document)
    # A default-mode session learning has the title but no Lesson-Type header.
    assert not cl.is_lesson_passage("# Session learning (session claude_1)\n\nRound totals.")
    assert not cl.is_lesson_passage("Session ID: claude_1\n\nQuestion: Lesson-Type: what is it?")


# -- Session start ---------------------------------------------------------------------


def test_session_start_lists_five_lessons_from_a_warm_cache(cl, monkeypatch, repo):
    head = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("x = 2\n")
    tagged = pricing(9, commit=head, files=["src/app.py"])
    wordy = pricing(8)
    wordy["body"] = "Pricing " + "word " * 80 + "end."
    baseline = lesson("Tests: 12 pass.", kind="state", key="test-baseline", date="2026-11-01")
    lessons = [tagged, wordy, *(pricing(n) for n in (7, 6, 5, 4)), baseline]
    asked = store(cl, monkeypatch, lessons)

    listed = cl.at_session_start(SESSION_KEY, str(repo))

    assert asked == [0]  # the cache only: nothing is fetched at session start
    assert listed["shown"] == 5 and listed["total"] == 7
    context = listed["context"].splitlines()
    assert context[0] == (
        "Cognee lessons for this project (5 of 7, from earlier sessions in the "
        "agent_sessions dataset). Check them before exploring:"
    )
    # No query: the baseline first, then the newest lessons.
    assert context[1] == "- Tests: 12 pass."
    short = git(repo, "rev-parse", "--short", head)
    assert context[2] == (
        f"- Pricing rule 9: totals are rounded. (session 2026-11-09, commit {short}; "
        "src/app.py changed since)"
    )
    assert len(context) == 6
    message = listed["message"].splitlines()
    assert message[0].endswith("agent_sessions dataset):")
    assert message[2] == context[2]  # date, commit and the marker reach the user too
    # The user's list is compact: a long statement is cut, the model's is whole.
    assert wordy["body"] in listed["context"]
    assert "word w…\n" in listed["message"] and wordy["body"] not in listed["message"]

    assert cl.start_list_shown(SESSION_KEY)
    items = cl.for_prompt(SESSION_KEY, "pricing rule", str(repo), deadline())
    assert [item["full"] for item in items if item["lesson"]["data_id"] == "id-9"] == [False]
    assert any(item["full"] for item in items)  # id-5 and id-4 were not listed


def test_session_start_with_a_cold_cache_lists_nothing_and_fetches_nothing(
    cl, suite, monkeypatch, tmp_path
):
    """The real listing: lessons exist on the server, no body is cached."""
    common = sys.modules["_plugin_common"]
    cm = cl._coding_memory
    requests: list = []

    def http(path, body=None, **kwargs):
        requests.append(path)
        if path == "/api/v1/datasets":
            return [{"name": "agent_sessions", "id": "dataset-id"}]
        return [
            {
                "id": f"00000000-0000-4000-8000-00000000000{n}",
                "externalMetadata": {"lesson_type": "state"},
            }
            for n in (1, 2)
        ]

    monkeypatch.setattr(common, "_json_http_request", http)
    monkeypatch.setattr(common, "dataset_id_for", lambda dataset: "")
    monkeypatch.setattr(cm, "_raw_text", lambda *a: pytest.fail("fetched a lesson body"))

    assert cl.at_session_start(SESSION_KEY, str(tmp_path)) == {}
    assert len(requests) == 2 and requests[1].startswith("/api/v1/datasets/dataset-id/data")
    assert not cl.start_list_shown(SESSION_KEY)

    # Warm (what ``send-unit.py --warm`` leaves): the same listing now has a body.
    cm._store_body("00000000-0000-4000-8000-000000000001", "Lesson-Type: state\n\nRound totals.")
    listed = cl.at_session_start(SESSION_KEY, str(tmp_path))
    assert listed["shown"] == 1 and listed["context"].endswith("\n- Round totals.")


@pytest.fixture
def session_start(suite, hook_module, monkeypatch):
    _claude_only(suite)
    module = hook_module(suite, "session-start.py")
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(module, "get_session_key", lambda: SESSION_KEY)
    import subprocess

    import _coding_memory

    monkeypatch.setattr(_coding_memory, "check_server_support", lambda **k: {"ok": True})
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    return module


def fake_lessons(monkeypatch, **overrides):
    marked: list = []
    stub = types.SimpleNamespace(
        start_list_shown=lambda key: False,
        at_session_start=lambda key, cwd: {
            "context": "LESSONS FOR MODEL",
            "message": "LESSONS FOR USER",
        },
        mark_shown=lambda key, lessons=(), block="", at_start=False: marked.append(block),
    )
    for name, value in overrides.items():
        setattr(stub, name, value)
    monkeypatch.setitem(sys.modules, "_coding_lessons", stub)
    return marked


def test_session_start_puts_the_list_in_the_context_and_the_message(session_start, monkeypatch):
    fake_lessons(monkeypatch)
    before = {
        "systemMessage": "CONNECTED",
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "NOTICE"},
    }
    result = session_start._coding_session_start(before, {"source": "startup", "cwd": "/repo"})
    assert result["hookSpecificOutput"]["additionalContext"] == "NOTICE\n\nLESSONS FOR MODEL"
    assert result["systemMessage"] == "CONNECTED\n\nLESSONS FOR USER"
    assert result["hookSpecificOutput"]["systemMessage"] == "LESSONS FOR USER"


def test_session_start_with_no_list_changes_nothing(session_start, monkeypatch):
    fake_lessons(monkeypatch, at_session_start=lambda key, cwd: {})
    before = {"systemMessage": "CONNECTED", "hookSpecificOutput": {"hookEventName": "SessionStart"}}
    assert session_start._coding_session_start(before, {"source": "startup"}) == before
    # A resumed session still holds the list it was given.
    fake_lessons(
        monkeypatch,
        start_list_shown=lambda key: True,
        at_session_start=lambda key, cwd: pytest.fail("listed twice"),
    )
    assert session_start._coding_session_start(before, {"source": "resume"}) == before


def test_the_block_given_after_a_compaction_is_remembered(session_start, monkeypatch):
    import _coding_memory

    marked = fake_lessons(monkeypatch, at_session_start=lambda *a: pytest.fail("listed"))
    monkeypatch.setattr(_coding_memory, "reset_delivery", lambda key: None)
    monkeypatch.setattr(_coding_memory, "last_request", lambda key: "fix it")
    monkeypatch.setattr(_coding_memory, "deliver", lambda key, request, cwd: "Lessons:\n- A.")
    result = session_start._coding_session_start({}, {"source": "compact"})
    assert result["hookSpecificOutput"]["additionalContext"] == "Lessons:\n- A."
    assert marked == ["Lessons:\n- A."]


# -- The prompt hook -------------------------------------------------------------------

LESSON_PASSAGE = (
    "Lesson-Type: state\nState-Key: footprint:rounding\n"
    "# Session learning (session claude_other)\n\nPricing totals are rounded."
)
PROMPT = "why is pricing rounded?"
#: The graph item as a cognee 1.6 server returns it for ``PROMPT``: the question
#: template around the retrieved context, two passages.
RECALLED = {
    "graph": [
        {
            "source": "graph",
            "text": f"The question is: `{PROMPT}`\nAnswer using this sectioned context. Keep the "
            "answer brief and do not use information outside the context.\n\nContext:\n"
            "`## Relevant passages\n"
            + LESSON_PASSAGE
            + "\n---\nSession ID: claude_other\n\nQuestion: what changed?\n\n"
            "Answer: the tax rate.`",
        }
    ]
}


@pytest.fixture
def lookup(suite, hook_module, monkeypatch):
    _claude_only(suite)
    module = load_lookup(suite, hook_module, monkeypatch)
    monkeypatch.setattr(module, "resolve_active_dataset_ids", lambda *a, **k: ("", []))
    monkeypatch.setattr(module, "get_dataset", lambda config: "agent_sessions")
    return module


def coding(lookup, monkeypatch, lines: list, full=None) -> types.SimpleNamespace:
    """Coding mode on, with ``lines`` as the prompt's lessons."""
    monkeypatch.setattr(lookup, "coding_memory_enabled", lambda: True)
    items = [
        {"lesson": {"data_id": f"id-{n}"}, "line": line, "full": full is None or full[n]}
        for n, line in enumerate(lines)
    ]
    real = __import__("_coding_lessons")
    stub = types.SimpleNamespace(
        PROMPT_TITLE=real.PROMPT_TITLE,
        is_lesson_passage=real.is_lesson_passage,
        asked=[],
        marked=[],
    )
    stub.for_prompt = lambda key, prompt, cwd, deadline: stub.asked.append(prompt) or items
    stub.mark_shown = lambda key, lessons=(), **k: stub.marked.extend(
        lesson["data_id"] for lesson in lessons
    )
    monkeypatch.setitem(sys.modules, "_coding_lessons", stub)
    return stub


def context_of(run) -> str:
    return run.output["hookSpecificOutput"]["additionalContext"]


def test_a_later_prompt_gets_recall_and_its_lessons(lookup, monkeypatch):
    lines = ["- Round totals. (session 2026-11-26)", "- Old. (shown earlier)"]
    stub = coding(lookup, monkeypatch, lines, [True, False])
    run = drive_recall(lookup, monkeypatch, recall=RECALLED, prompt=PROMPT)
    context = context_of(run)
    assert stub.asked == [PROMPT]
    assert sorted(run.calls) == ["graph", "session_context"]  # the recall of normal mode
    assert "; lessons 2;" in context.splitlines()[0]  # then the ledger's own counts
    assert (
        "=== Lessons for this prompt (Cognee) ===\n- Round totals. (session 2026-11-26)\n"
        "- Old. (shown earlier)"
    ) in context
    # Above the recall section, which keeps its other passages.
    assert context.index("Lessons for this prompt") < context.index("Question: what changed?")
    # The lesson document is in the list, not a second time among the passages.
    assert "Lesson-Type: state" not in context
    # Only what was printed in full is remembered as shown.
    assert stub.marked == ["id-0"]
    assert run.detail("context_lookup_hit")["lessons"] == 2


def test_lessons_without_any_recall_still_reach_the_prompt(lookup, monkeypatch):
    coding(lookup, monkeypatch, ["- Round totals."])
    context = context_of(drive_recall(lookup, monkeypatch, prompt="pricing rounding"))
    assert "- Round totals." in context and "no memory matches" not in context


def test_a_prompt_with_no_lesson_has_no_list(lookup, monkeypatch):
    coding(lookup, monkeypatch, [])
    context = context_of(drive_recall(lookup, monkeypatch, recall=RECALLED, prompt=PROMPT))
    assert "Lessons for this prompt" not in context and "lessons" not in context.splitlines()[0]
    assert "Lesson-Type: state" not in context  # lessons are never ordinary passages here
    assert "Question: what changed?" in context


def test_lessons_over_the_budget_are_left_out_and_counted(lookup, monkeypatch):
    lines = [f"- Lesson {n}: " + "x" * 170 for n in range(5)]
    stub = coding(lookup, monkeypatch, lines)
    monkeypatch.setenv("COGNEE_INJECT_MAX_CHARS", "1024")
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "false")
    run = drive_recall(lookup, monkeypatch, recall=RECALLED, prompt=PROMPT)
    context = context_of(run)
    kept = [line for line in lines if line in context]
    assert 0 < len(kept) < 5
    assert kept == lines[: len(kept)]  # the most relevant ones stay
    assert f"; lessons {len(kept)} ({5 - len(kept)} left out to fit)" in context.splitlines()[0]
    assert stub.marked == [f"id-{n}" for n in range(len(kept))]
    assert len(json.dumps(run.output)) + 1 <= 1024


def test_outside_coding_mode_recall_is_unchanged(lookup, monkeypatch):
    stub = coding(lookup, monkeypatch, ["- Round totals."])
    monkeypatch.setattr(lookup, "coding_memory_enabled", lambda: False)
    context = context_of(drive_recall(lookup, monkeypatch, recall=RECALLED, prompt=PROMPT))
    assert stub.asked == [] and "Lessons for this prompt" not in context
    assert "Lesson-Type: state" in context  # a lesson document is an ordinary passage


def run_main(lookup, monkeypatch, capsys, prompt: str) -> list:
    payload = {"session_id": SESSION_KEY, "prompt": prompt, "cwd": "/repo"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    lookup.main()
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


@pytest.fixture
def main_seams(lookup, monkeypatch):
    """``main`` in coding mode with the block, the recall and the notice faked."""
    monkeypatch.setattr(lookup, "coding_memory_enabled", lambda: True)
    monkeypatch.setattr(lookup, "hook_log", lambda *a, **k: None)
    seams = types.SimpleNamespace(block="", start_list=False, recalled=[], delivered=[], marked=[])

    async def fake_run(prompt, cwd="", notice=""):
        seams.recalled.append(prompt)
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "RECALL",
            }
        }

    monkeypatch.setattr(lookup, "_run", fake_run)
    monkeypatch.setattr(lookup, "_with_review_notice", lambda output, cwd: output)
    monkeypatch.setattr(lookup, "with_base_url_notice", lambda *a, **k: None)
    monkeypatch.setattr(lookup, "_coding_server_supported", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "_coding_memory",
        types.SimpleNamespace(
            begin_turn=lambda *a, **k: None,
            deliver=lambda key, prompt, cwd: seams.delivered.append(prompt) or seams.block,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "_coding_lessons",
        types.SimpleNamespace(
            start_list_shown=lambda key: seams.start_list,
            mark_shown=lambda key, lessons=(), block="", **k: seams.marked.append(block),
        ),
    )
    return seams


def test_the_first_prompts_block_is_its_memory_and_later_prompts_are_recalled(
    lookup, main_seams, monkeypatch, capsys
):
    main_seams.block = "Project memory (Cognee): BLOCK"
    (first,) = run_main(lookup, monkeypatch, capsys, "Fix the parser")
    assert first["hookSpecificOutput"]["additionalContext"] == "Project memory (Cognee): BLOCK"
    assert main_seams.recalled == [] and main_seams.marked == ["Project memory (Cognee): BLOCK"]

    main_seams.block = ""  # delivered: ``deliver`` answers "" from now on
    (second,) = run_main(lookup, monkeypatch, capsys, "Now the lexer")
    assert second["hookSpecificOutput"]["additionalContext"] == "RECALL"
    assert main_seams.recalled == ["Now the lexer"]


def test_after_a_session_start_list_the_first_prompt_is_recalled(
    lookup, main_seams, monkeypatch, capsys
):
    main_seams.start_list, main_seams.block = True, "BLOCK"
    (output,) = run_main(lookup, monkeypatch, capsys, "Fix the parser")
    assert output["hookSpecificOutput"]["additionalContext"] == "RECALL"
    assert main_seams.delivered == []  # no block: its lessons are in the context already


def test_a_server_too_old_for_coding_mode_is_not_recalled(lookup, main_seams, monkeypatch, capsys):
    monkeypatch.setattr(lookup, "_coding_server_supported", lambda: False)
    assert run_main(lookup, monkeypatch, capsys, "Fix the parser") == []
    assert main_seams.recalled == []


def test_a_short_prompt_still_gets_a_pending_review_notice(lookup, main_seams, monkeypatch, capsys):
    notice = {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "REVIEW"}
    }
    monkeypatch.setattr(lookup, "_with_review_notice", lambda output, cwd: output or notice)
    assert run_main(lookup, monkeypatch, capsys, "ok") == [notice]
    assert main_seams.recalled == []
    # Outside coding mode a short prompt prints nothing, as before.
    monkeypatch.setattr(lookup, "coding_memory_enabled", lambda: False)
    assert run_main(lookup, monkeypatch, capsys, "ok") == []


# -- What stays off --------------------------------------------------------------------


class _Unread:
    def read(self):
        pytest.fail("the hook read its payload in coding mode")


@pytest.mark.parametrize("script", ["store-to-session.py", "pre-compact.py"])
def test_per_call_capture_and_the_anchor_stay_off(suite, hook_module, monkeypatch, capsys, script):
    _claude_only(suite)
    module = hook_module(suite, script)
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    monkeypatch.setattr(sys, "stdin", _Unread())
    module.main()
    assert capsys.readouterr().out == ""
