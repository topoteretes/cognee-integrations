"""``_recall_text``: the only_context item, cut down to the retrieved context.

Shapes here mirror real cognee 1.6.1 output captured from a live server (the
transcripts themselves are not reproduced — they are the user's sessions):

  * the item is ``[history]`` + question template + ``\\`context\\``` +
    ``[guidance block]``, with three possible templates;
  * passages are joined with ``\\n---\\n``, which a markdown rule inside an
    assistant answer also produces — one real chunk came back in six pieces;
  * a bridged chunk opens with ``Session ID: <id>`` or
    ``# Session learning — <date> (session <id>)``; one chunk was 21k chars;
  * entity bullets carry ``X is a concept.`` and ``X belongs to set …`` lines
    that say nothing (15–40% of entity lines in the samples).

Contract:
  * the context is found by the prompt marker and the final backtick (or the
    guidance title), never by counting backticks — the context is full of them;
  * anything unparsed is returned whole and flagged;
  * own-session passages go, others stay; the cap cuts at a paragraph break and
    says how much it cut; pruning never touches a bullet with content; facts
    are untouched; the budget trims passages only;
  * ``code_facts`` tells an empty ``query_facts`` payload from a hit.

Every suite ships the module byte-identical, so all are exercised.
"""

from __future__ import annotations

import pytest

PROMPT = "why does `recall_via_http` pop the session_id? Context: federated reads"
SID = "claude_18c06095-db5a-4356-9d7b-16127c2f3190"
OTHER = "codex_01a0cdda-8d14-7291-bae3-c7eeb7fce251"

HYBRID_HEAD = (
    f"The question is: `{PROMPT}`\n"
    "Answer using this sectioned context. Keep the answer brief and do not use "
    "information outside the context.\n\nContext:\n`"
)
GRAPH_HEAD = (
    f"The question is: `{PROMPT}`\n"
    "and here is the context provided with a set of relationships from a knowledge "
    "graph separated by \\n---\\n each represented as node1 -- relation -- node2 triplet: `"
)
PLAIN_HEAD = f"The question is: `{PROMPT}`\nAnd here is the context: `"
GUIDANCE = (
    "## Active session guidance\nWhen guidance conflicts, prefer the later item.\n- Run ruff."
)

OWN_PASSAGE = (
    f"Session ID: {SID}\n\nQuestion: earlier\n\nAnswer: run `uv sync` then:\n\n---\n\n- step"
)
OWN_NOTE = (
    f"# Session learning — 2026-09-18 (session {SID})\n\n"
    "The dify-sdk plugin lives in integrations/dify-sdk."
)
OTHER_PASSAGE = (
    f"Session ID: {OTHER}\n\nQuestion: other\n\nAnswer: use `--graph`; see ```python\nx = 1\n```."
)
BLOG_PASSAGE = "Cognee blog: memory for agents.\n\nParagraph two of the post."

ENTITIES = (
    "### recalled context injection\n"
    "- recalled context injection is a concept.\n"
    "- recalled context injection is a process.\n"
    f"- recalled context injection belongs to set session_learnings:{OTHER}.\n"
    "- recalled context injection belongs to set session_learnings.\n"
    "- Document chunk mentions recalled context injection: injecting recalled context.\n"
    "\n"
    "### empty node\n"
    "- empty node is a node.\n"
    "- empty node belongs to set user_sessions_from_cache.\n"
    "\n"
    "### scheduled task\n"
    "- scheduled task is a dependency of the nightly sync.\n"
)
FACTS = "- cognee-integrations uses only_context on every prompt.\n- top_k stays at 5."


def _context(passages: list[str], entities: str = ENTITIES, facts: str = FACTS) -> str:
    return (
        "## Relevant passages\n"
        + "\n---\n".join(passages)
        + "\n\n## Relevant entities\n"
        + entities
        + "\n## Related facts\n"
        + facts
    )


@pytest.fixture
def rt(suite, isolated_modules):
    return isolated_modules(suite, "_recall_text")


# ── step 2: the template goes ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "head", [HYBRID_HEAD, GRAPH_HEAD, PLAIN_HEAD], ids=["hybrid", "graph", "plain"]
)
def test_the_context_is_found_in_every_template(rt, head):
    context = _context([OTHER_PASSAGE])
    layers = rt.split_only_context(f"{head}{context}`", PROMPT)
    assert layers["parsed"] is True
    assert layers["context"] == context
    assert layers["history"] == "" and layers["guidance"] == ""


def test_backticks_in_prompt_and_context_do_not_matter(rt):
    """The prompt has backticks and the word ``Context:``; the context has code
    fences. Only the prompt marker and the final backtick are anchors."""
    context = _context([OTHER_PASSAGE, OWN_PASSAGE])
    assert context.count("`") > 4
    layers = rt.split_only_context(f"{HYBRID_HEAD}{context}`", PROMPT)
    assert layers["parsed"] and layers["context"] == context


def test_history_and_a_trailing_guidance_block_are_split_off(rt):
    history = "Previous conversation:\n\n[2026-10-01]\nQUESTION: earlier\nANSWER: `done`\n\n"
    context = _context([OTHER_PASSAGE])
    text = f"{history}{HYBRID_HEAD}{context}`\n\n{GUIDANCE}"
    layers = rt.split_only_context(text, PROMPT)
    assert layers["parsed"]
    assert layers["history"] == history.rstrip()
    assert layers["context"] == context
    assert layers["guidance"] == GUIDANCE


def test_a_guidance_title_quoted_inside_a_passage_is_not_the_end(rt):
    """Seen live: a passage discussing the guidance block. The text still ends
    with the context's backtick, so that is the end."""
    quoted = (
        f"Session ID: {OTHER}\n\nAnswer: the block is titled\n\n"
        "## Active session guidance\nand follows the context."
    )
    context = _context([quoted])
    layers = rt.split_only_context(f"{HYBRID_HEAD}{context}`", PROMPT)
    assert layers["parsed"] and layers["context"] == context and layers["guidance"] == ""


@pytest.mark.parametrize(
    "text",
    [
        "bare context from a pre-1.6.0 server",
        "The question is: `a rewritten question`\n\nContext:\n`c`",
        f"The question is: `{PROMPT}`\nno context slot follows",
        f"{HYBRID_HEAD}unterminated context\n\n## Something else",
    ],
    ids=["legacy", "rewritten-question", "no-slot", "no-end"],
)
def test_anything_else_is_returned_whole_and_flagged(rt, text):
    layers = rt.split_only_context(text, PROMPT)
    assert layers["parsed"] is False
    assert layers["context"] == text
    trimmed, guidance, stats = rt.trim_recall_text(text, PROMPT, SID)
    assert trimmed == text and guidance == ""
    assert stats["parsed"] is False and stats["chars_out"] == len(text)


# ── step 3: the context shrinks ───────────────────────────────────────────────


def test_markdown_rules_inside_a_passage_do_not_split_it(rt):
    """``OWN_PASSAGE`` carries a horizontal rule; the piece after it has no
    document header and is glued back on."""
    passages = rt.split_passages("\n---\n".join([OWN_PASSAGE, OTHER_PASSAGE, OWN_NOTE]))
    assert passages == [OWN_PASSAGE, OTHER_PASSAGE, OWN_NOTE]


def test_own_session_passages_are_dropped_by_either_header(rt):
    assert rt.passage_session_id(OWN_PASSAGE) == SID
    assert rt.passage_session_id(OWN_NOTE) == SID
    assert rt.passage_session_id(OTHER_PASSAGE) == OTHER
    assert rt.passage_session_id(BLOG_PASSAGE) == ""

    text, stats = rt.minimize_context(
        _context([OWN_PASSAGE, OTHER_PASSAGE, OWN_NOTE, BLOG_PASSAGE]), SID, passage_chars=0
    )
    assert stats["passages_in"] == 4 and stats["passages_dropped"] == 2
    assert OTHER_PASSAGE in text and BLOG_PASSAGE in text
    assert "dify-sdk plugin lives" not in text and "Question: earlier" not in text


def test_without_a_session_id_nothing_is_dropped(rt):
    text, stats = rt.minimize_context(_context([OWN_PASSAGE, OTHER_PASSAGE]), "", passage_chars=0)
    assert stats["passages_dropped"] == 0
    assert OWN_PASSAGE in text and OTHER_PASSAGE in text


def test_a_long_passage_is_capped_at_a_paragraph_break_and_marked(rt):
    paragraphs = [f"Paragraph {i}: " + "x" * 90 for i in range(40)]
    long = f"Session ID: {OTHER}\n\n" + "\n\n".join(paragraphs)
    capped = rt.cap_passage(long, 1000)
    head, marker = capped.rsplit("\n", 1)
    assert len(head) <= 1000 and head.endswith("x"), "cut at a paragraph boundary"
    assert marker.startswith("…[+") and marker.endswith(" chars]")
    assert len(head) + int(marker[3:-7]) == len(long)
    assert rt.cap_passage(long, 0) == long, "0 disables the cap"
    assert rt.cap_passage("short", 1000) == "short"


def test_the_cap_is_applied_per_surviving_passage(rt):
    long = f"Session ID: {OTHER}\n\n" + "\n\n".join("y" * 80 for _ in range(50))
    text, stats = rt.minimize_context(_context([long, OTHER_PASSAGE]), SID, passage_chars=500)
    assert stats["passages_capped"] == 1
    assert "…[+" in text and OTHER_PASSAGE in text


def test_entity_bullets_without_content_are_pruned_and_the_rest_kept(rt):
    pruned, removed = rt.prune_entities(ENTITIES)
    # 2 type + 2 set lines under the first entity, 2 under "empty node" plus its
    # heading: 7 removed.
    assert removed == 7
    assert "is a concept." not in pruned and "belongs to set" not in pruned
    assert "### empty node" not in pruned, "a heading left without bullets goes too"
    assert (
        "- Document chunk mentions recalled context injection: injecting recalled context."
        in pruned
    )
    assert "- scheduled task is a dependency of the nightly sync." in pruned, "a real fact stays"
    assert "### scheduled task" in pruned


def test_an_entities_block_pruned_to_nothing_disappears(rt):
    only_noise = "### n\n- n is a concept.\n- n belongs to set s.\n"
    text, stats = rt.minimize_context(
        _context([OTHER_PASSAGE], entities=only_noise), SID, passage_chars=0
    )
    assert "## Relevant entities" not in text
    assert stats["entity_lines_removed"] == 3
    assert "## Related facts\n" + FACTS in text


def test_facts_are_untouched_and_sections_keep_their_order(rt):
    text, _ = rt.minimize_context(_context([OTHER_PASSAGE]), SID, passage_chars=0)
    assert (
        text.index("## Relevant passages")
        < text.index("## Relevant entities")
        < text.index("## Related facts")
    )
    assert text.endswith(FACTS)


def test_the_budget_trims_passages_from_the_end_and_nothing_else(rt):
    passages = [f"Session ID: {OTHER}_{i}\n\nAnswer {i}: " + "z" * 400 for i in range(6)]
    text, stats = rt.minimize_context(_context(passages), SID, passage_chars=0, context_chars=1500)
    assert stats["budget_trimmed"] >= 3
    assert "Answer 0:" in text, "the highest-ranked passage survives"
    assert "Answer 5:" not in text
    assert "## Relevant entities" in text and FACTS in text
    assert len(text) <= 1500


def test_an_unsectioned_context_passes_through(rt):
    plain = "node1 -- relation -- node2\n---\nnode3 -- relation -- node4"
    text, stats = rt.minimize_context(plain, SID)
    assert text == plain
    assert stats["passages_in"] == 0


def test_a_global_context_preamble_is_kept(rt):
    text, _ = rt.minimize_context(
        "Global: the project is cognee.\n\n" + _context([OTHER_PASSAGE]), SID, passage_chars=0
    )
    assert text.startswith("Global: the project is cognee.\n\n## Relevant passages")


def test_trim_recall_text_reports_both_steps(rt):
    history = "Previous conversation:\n\nQUESTION: q\nANSWER: a\n\n"
    text = f"{history}{HYBRID_HEAD}{_context([OWN_PASSAGE, OTHER_PASSAGE])}`\n\n{GUIDANCE}"
    context, guidance, stats = rt.trim_recall_text(text, PROMPT, SID, passage_chars=0)
    assert guidance == GUIDANCE
    assert "Question: earlier" not in context and OTHER_PASSAGE in context
    assert stats["parsed"] and stats["history_chars"] == len(history.rstrip())
    assert stats["passages_dropped"] == 1 and stats["chars_out"] == len(context) < stats["chars_in"]


# ── the knobs ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 2000), ("1500", 1500), ("0", 0), ("junk", 2000), ("-5", 2000)],
)
def test_the_passage_cap_knob(rt, monkeypatch, raw, expected, suite):
    if expected == 2000 and suite.name == "claude-code":
        expected = 1200
    monkeypatch.setenv("COGNEE_RECALL_PASSAGE_CHARS", raw)
    assert rt.passage_chars_limit() == expected


def test_the_budget_knob_default(rt, monkeypatch, suite):
    monkeypatch.delenv("COGNEE_RECALL_CONTEXT_CHARS", raising=False)
    assert rt.context_chars_limit() == (0 if suite.name == "claude-code" else 12000)
    monkeypatch.setenv("COGNEE_RECALL_CONTEXT_CHARS", "0")
    assert rt.context_chars_limit() == 0


# ── the code lane ─────────────────────────────────────────────────────────────


FACT = {
    "id": "e22d",
    "kind": "symbol",
    "name": "scripts/_recall_http.coerce_top_k",
    "file": "scripts/_recall_http.py",
    "line": 120,
    "symbol_kind": "function",
    "relations": [{"type": "declares", "target_id": "9890", "target": "scripts"}],
}


def test_code_facts_tells_an_empty_payload_from_a_hit(rt):
    empty = (
        '{"operation": "query_facts", "facts": [], "total": 0, "offset": 0, '
        '"limit": 5, "has_more": false}'
    )
    assert rt.code_facts(empty) == []
    assert rt.code_facts('{"operation": "query_facts", "facts": [{"name": "f"}]}') == [
        {"name": "f"}
    ]
    assert rt.code_facts("process_payment (function) — billing/pay.py:42") is None
    assert rt.code_facts('{"operation": "impact_analysis", "seeds": []}') is None


def test_code_facts_render_one_line_per_symbol(rt):
    rendered = rt.format_code_facts([FACT, "junk", {"name": "bare"}])
    assert rendered.splitlines() == [
        "- scripts/_recall_http.coerce_top_k [function] scripts/_recall_http.py:120",
        "  → declares scripts",
        "- bare",
    ]
