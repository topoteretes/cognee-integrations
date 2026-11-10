"""Crediting Cognee memory in the answer (SDK-1071).

The recall hook marks every injected item with where it came from and asks the
agent to end an answer that used memory with one credit line (its form differs
per host). The recall hook records the turn it injected memory on, and the Stop
hook counts a credit when the answer on that turn ends with the credit line.

Contract:
  * a graph item is marked with the dataset the server says it came from,
    ``[dataset <name>]``, and never with a document (SDK-1393); an item with no
    dataset (an older server, or another source) keeps its origin mark;
  * graph memory is marked per passage, plus one mark each for its entities
    and facts blocks;
  * the instruction says how many memories this prompt found: the items whose
    opening text reaches the context, after any budget and dedupe; with none,
    there is no instruction;
  * it keeps the longer first-credit wording until an answer has credited
    memory once;
  * every prompt starts a new turn before recall can return early, so a credit
    on a turn that injected nothing is logged, never counted;
  * ``COGNEE_PLUGIN_CITE_MEMORY=off`` turns all of it off, and Codex starts
    with it off.
"""

from __future__ import annotations

import json
import re

import pytest
from utils.recall import drive_recall

SESSION_KEY = "sess-credit-1"
CODEX_SID = "codex_01a0cdda-8d14-7291-bae3-c7eeb7fce251"
CLAUDE_SID = "claude_18c06095-db5a-4356-9d7b-16127c2f3190"

PASSAGE_CODEX = f"Session ID: {CODEX_SID}\n\nQuestion: CLI or server?\n\nAnswer: prefer the server."
PASSAGE_LEARNING = (
    f"# Session learning — 2026-10-03 (session {CLAUDE_SID})\n\nRun ruff before committing."
)
PASSAGE_DOC = "Cognee blog: memory for agents."
CONTEXT = (
    "## Relevant passages\n"
    + "\n---\n".join([PASSAGE_CODEX, PASSAGE_LEARNING, PASSAGE_DOC])
    + "\n\n## Relevant entities\n### server\n- server is preferred over the CLI.\n"
    + "\n## Related facts\n- the plugin prefers the server."
)
HIT = {"graph": [{"source": "graph", "text": CONTEXT}]}
NAMED = "source: maya-7f3a-f50bfe3707aae01a3d822a46fb5e87ad\nMaya ordered EU 39."


def _graph(dataset: str, text: str) -> dict:
    return {"source": "graph", "dataset_name": dataset, "text": text}


#: Two results from one dataset: a passage, entities and facts, then entities
#: and facts again. Five items, four of them blocks with the same mark.
TWO_RESULTS = {
    "graph": [
        _graph(
            "ds1",
            f"## Relevant passages\n{NAMED}\n\n## Relevant entities\n### maya\n- Maya is a "
            "customer.\n\n## Related facts\n- Maya ordered shoes.",
        ),
        _graph(
            "ds1",
            "## Relevant entities\n### order\n- the order shipped.\n\n"
            "## Related facts\n- the order was returned.",
        ),
    ]
}


@pytest.fixture
def rt(suite, isolated_modules):
    return isolated_modules(suite, "_recall_text")


def _noun(n: int) -> str:
    return "1 memory" if n == 1 else f"{n} memories"


# ── marks ──────────────────────────────────────────────────────────────────


def test_item_mark_names_the_dataset_or_the_origin(rt):
    assert rt.item_mark("document", "ds1") == "[dataset ds1]"
    assert rt.item_mark("agent trace, recall") == "[agent trace, recall]"
    assert rt.item_mark("") == ""


@pytest.fixture
def marker(suite, rt):
    if not hasattr(rt, "mark_graph"):
        pytest.skip(f"{suite.name}: graph memory is marked in the recall hook")
    return rt


def test_graph_memory_is_marked_with_its_dataset(marker):
    keys: list = []
    named = "## Relevant passages\n" + "\n---\n".join([NAMED, PASSAGE_CODEX])
    text = marker.mark_graph(named + "\n\n## Related facts\n- x", "alice", keys)
    assert f"[dataset alice]\n{NAMED}" in text
    assert f"[dataset alice]\n{PASSAGE_CODEX}" in text
    assert "## Related facts [dataset alice]\n- x" in text
    assert "document" not in text and "[m1" not in text
    assert len(keys) == 3 and all(key in text for key in keys)


def test_graph_memory_without_a_dataset_keeps_its_origin(marker):
    keys: list = []
    text = marker.mark_graph(CONTEXT, "", keys)
    assert f"[Codex session]\n{PASSAGE_CODEX}" in text
    assert f"[Claude Code session learning, 2026-10-03]\n{PASSAGE_LEARNING}" in text
    assert f"[document]\n{PASSAGE_DOC}" in text
    assert "## Relevant entities [knowledge graph entities]\n### server" in text
    assert "## Related facts [knowledge graph facts]\n- the plugin prefers" in text
    assert len(keys) == 5


def test_unsectioned_context_is_one_item(marker):
    keys: list = []
    assert marker.mark_graph("bare context", "", keys) == "[Cognee memory]\nbare context"
    assert marker.mark_graph("  ", "", keys) == "  "
    assert keys == ["[Cognee memory]\nbare context"]


def test_codex_instruction_asks_for_the_braille_credit(suite, rt):
    if suite.name != "codex":
        pytest.skip("only Codex asks the agent to write the braille logotype")
    text = rt.credit_instruction(first_in_session=False, found=2)
    upper, lower = rt.CREDIT_LOGOTYPE
    credit = f"> `{upper}`  <what you remembered>,\n> `{lower}`  from the <dataset> dataset"
    assert credit in text
    assert "Cognee found 2 memories for this prompt" in text
    assert "as [dataset <name>]" in text
    assert "<document>" not in text and "[mN]" not in text and "\U0001f9e0" not in text


def test_codex_chatgpt_app_credit_opens_with_the_katex_icon(suite, rt):
    if suite.name != "codex":
        pytest.skip("only Codex tells the ChatGPT app from the terminal")
    text = rt.credit_instruction(first_in_session=False, found=2, chatgpt_app=True)
    # One line, which the app wraps where it needs to: the icon, two em spaces,
    # then what and where from.
    icon = f"> {rt.KATEX_ICON}{rt.ICON_PADDING}"
    one_line = f"{icon}<what you remembered>, from the <dataset> dataset\n"
    assert one_line in text
    assert "\\\n" not in text  # no hard break
    assert rt.CREDIT_LOGOTYPE[0] not in text and "\U0001f9e0" not in text
    # The first credit says how it connects to earlier work, as the terminal's does.
    first = rt.credit_instruction(first_in_session=True, found=2, chatgpt_app=True)
    assert "first Cognee credit" in first
    # The terminal keeps the braille rows.
    assert rt.CREDIT_LOGOTYPE[0] in rt.credit_instruction(first_in_session=False, found=2)


def test_codex_katex_icon_is_the_approved_waveform(suite, rt):
    """The icon as approved in the ChatGPT app: five Lavender strokes, shortest
    at the edges, 0.11 em wide and 0.1 em apart, the tallest 1.1 em."""
    if suite.name != "codex":
        pytest.skip("only Codex draws the icon in KaTeX")
    assert rt.KATEX_ICON == (
        r"$\color{#BC9BFF}{\rule[0.19em]{0.11em}{0.42em}\kern{0.1em}"
        r"\rule[-0.03em]{0.11em}{0.86em}\kern{0.1em}\rule[-0.15em]{0.11em}{1.1em}"
        r"\kern{0.1em}\rule[-0.03em]{0.11em}{0.86em}\kern{0.1em}"
        r"\rule[0.19em]{0.11em}{0.42em}}$"
    )


def test_codex_reads_the_client_from_the_transcript(suite, rt, tmp_path, monkeypatch):
    if suite.name != "codex":
        pytest.skip("only Codex tells the ChatGPT app from the terminal")
    sid = "01a1ab13-1ad4-7c70-9d22-dfb8f71eee33"
    day = tmp_path / "codex" / "sessions" / "2026" / "11" / "05"
    day.mkdir(parents=True)
    rollout = day / f"rollout-2026-11-05T10-38-39-{sid}.jsonl"
    meta = {"type": "session_meta", "payload": {"id": sid, "originator": "Codex Desktop"}}
    rollout.write_text(json.dumps(meta) + "\n{}\n", encoding="utf-8")
    state = tmp_path / "state"
    # Named by the payload.
    assert rt.session_originator(state, "k1", str(rollout)) == rt.DESKTOP_ORIGINATOR
    # Found by the session id under CODEX_HOME, and kept for the session.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert rt.session_originator(state, "k2", session_id=sid) == "Codex Desktop"
    rollout.unlink()
    assert rt.session_originator(state, "k2", session_id=sid) == "Codex Desktop"
    # Nothing to read: unknown, so the terminal's credit.
    unknown = "01a1ffff-0000-0000-0000-000000000000"
    assert rt.session_originator(state, "k3", session_id=unknown) == ""


@pytest.mark.parametrize(
    ("ending", "credited"),
    [
        (
            "> `⡏⠁⡏⡇⡏⡁⡧⣸⢸⠅⡯`  Alice chased the Rabbit,\n> `⠉⠁⠉⠁⠉⠁⠁⠈⠈⠁⠉`  from the a dataset",
            True,
        ),
        (
            "> ⡏⠁⡏⡇⡏⡁⡧⣸⢸⠅⡯  Alice chased the Rabbit,\n> ⠉⠁⠉⠁⠉⠁⠁⠈⠈⠁⠉  from the a dataset",
            True,
        ),
        ("> Cognee memory: Alice chased the Rabbit, from the a dataset", True),
        ("> $\\color{#BC9BFF}{\\rule{0.18em}{0.9em}}$ Alice chased the Rabbit,\\\n> from a", True),
        ("The cost is $\\frac{1}{2}$ of it.", False),
        ("See [the site](https://www.cognee.ai) for more.", False),
        ("> \U0001f9e0 Cognee memory: an older answer", True),
        ("The logo ⡏⠁⡏⡇⡏⡁⡧⣸⢸⠅⡯ in passing.\nNothing else.", False),
    ],
)
def test_codex_counts_the_credit(suite, rt, ending, credited):
    if suite.name != "codex":
        pytest.skip("Codex credits with the braille logotype")
    result = rt.check_credits(f"Alice is curious.\n\n{ending}", True)
    assert result["credited"] is credited and result["injected"] is True


@pytest.mark.parametrize(
    ("answer", "credited"),
    [
        ("done.\n> Cognee memory: you chose the server, from the ds1 dataset", True),
        ("ok\n> \U0001f9e0 Cognee memory: her orders", True),  # an older answer's brain
        ("> Cognee memory: x\n\nMore text.", False),  # not the last line
        ("No memory here.", False),
        ("use `arr[m1]`; cite several tags as [m1, m3]", False),
    ],
)
def test_check_credits(suite, rt, answer, credited):
    if suite.name == "cursor":
        pytest.skip("cursor: the credit line carries the logo")
    assert rt.check_credits(answer, True) == {"injected": True, "credited": credited}
    assert rt.check_credits(answer, False) == {"injected": False, "credited": credited}


def test_credit_state_round_trip(rt, tmp_path):
    assert not rt.has_credited(tmp_path, SESSION_KEY)
    rt.record_credit(tmp_path, SESSION_KEY, {"injected": True, "credited": False})
    assert not rt.has_credited(tmp_path, SESSION_KEY)
    # A credit on a turn that injected nothing is not counted.
    rt.record_credit(tmp_path, SESSION_KEY, {"injected": False, "credited": True})
    assert not rt.has_credited(tmp_path, SESSION_KEY)
    rt.record_credit(tmp_path, SESSION_KEY, {"injected": True, "credited": True})
    assert rt.has_credited(tmp_path, SESSION_KEY)
    assert (tmp_path / "sessions" / SESSION_KEY / "credit.json").is_file()


def test_an_injected_turn_lasts_until_the_next_prompt(rt, tmp_path):
    assert not rt.injected_this_turn(tmp_path, SESSION_KEY)
    rt.begin_turn(tmp_path, SESSION_KEY)
    assert not rt.injected_this_turn(tmp_path, SESSION_KEY)
    rt.mark_injected(tmp_path, SESSION_KEY)
    assert rt.injected_this_turn(tmp_path, SESSION_KEY)
    rt.begin_turn(tmp_path, SESSION_KEY)
    assert not rt.injected_this_turn(tmp_path, SESSION_KEY)


@pytest.mark.parametrize(
    ("answer", "credited"),
    [
        ("done.\n> [​](https://www.cognee.ai) On Oct 3 you chose the server.", True),
        ("> [](https://cognee.ai/) you chose the server.", True),  # zero-width space dropped
        ("  >  [​](https://www.cognee.ai) indented quote", True),
        ("a plain answer", False),
        ("> [​](https://www.cognee.ai)", False),  # the logo with nothing after it
        ("see [​](https://www.cognee.ai) inline", False),  # not a quoted credit line
        ("> [Cognee](https://www.cognee.ai) docs", False),
    ],
)
def test_cursor_check_credits(suite, rt, answer, credited):
    if suite.name != "cursor":
        pytest.skip("only cursor credits with the logo alone")
    assert rt.check_credits(answer, True) == {"injected": True, "credited": credited}


def test_cursor_instruction_asks_for_the_logo_alone(suite, rt):
    if suite.name != "cursor":
        pytest.skip("only cursor credits with the logo alone")
    text = rt.credit_instruction(first_in_session=False, found=2, dataset="agent_sessions")
    assert f"> {rt.LOGOTYPE} <what you remembered>, based on the agent_sessions dataset." in text
    assert "as [dataset <name>]" in text
    assert "[mN" not in text and "[m1, m3]" not in text


@pytest.mark.parametrize(
    ("dataset", "source"),
    [
        ("agent_sessions", "the agent_sessions dataset"),
        # By UUID the agent names the dataset itself rather than print the UUID.
        ("821104a2-2961-5854-82ec-ab6ef24fccec", "the <name of the dataset you searched> dataset"),
        ("", "Cognee memory"),
    ],
)
def test_cursor_credit_names_the_dataset(suite, rt, dataset, source):
    if suite.name != "cursor":
        pytest.skip("only cursor names the dataset in the credit line")
    assert rt.credit_source(dataset) == source
    assert f"based on {source}." in rt.search_credit_header(True, dataset)
    assert f"Saved to {source}:" in rt.save_receipt_header(True, dataset)


CURSOR_INSTRUCTION_KW = {"first_in_session": True, "found": 7}


@pytest.mark.parametrize(
    ("text", "stored"),
    [
        (
            "Mika likes Yuji.\n\n> [\u200b](https://www.cognee.ai) Mika likes Yuji.",
            "Mika likes Yuji.",
        ),
        ("Mika likes Yuji.\n> 🧠 Cognee memory [m1, m6]: from the graph.", "Mika likes Yuji."),
        (
            "Yuji.\n\n> ⡏⠁⡏⡇⡏⡁⡧⣸⢸⠅⡯ Cognee memory [m1]: the graph.\\\n> ⠉⠁⠉⠁⠉⠁⠁⠈⠈⠁⠉",
            "Yuji.",
        ),
        ("a plain answer", "a plain answer"),
        ("see https://www.cognee.ai for docs", "see https://www.cognee.ai for docs"),
        ("Cognee memory [m1] in prose stays", "Cognee memory [m1] in prose stays"),
    ],
)
def test_cursor_strip_credit(suite, rt, text, stored):
    if suite.name != "cursor":
        pytest.skip("only cursor strips credit text before storing")
    assert rt.strip_credit(text) == stored


def test_cursor_strip_credit_cuts_the_pasted_instruction(suite, rt):
    if suite.name != "cursor":
        pytest.skip("only cursor strips credit text before storing")
    pasted = f"why no logo?\n\n{rt.credit_instruction(**CURSOR_INSTRUCTION_KW)}\n\nthanks"
    assert rt.strip_credit(pasted) == "why no logo?\n\nthanks"


def test_cursor_search_credit_header(suite, rt):
    if suite.name != "cursor":
        pytest.skip("only cursor heads its search output with the credit instruction")
    header = rt.search_credit_header(True)
    assert "\n" not in header and rt.LOGOTYPE in header
    assert "Cognee memory [" not in header
    # Off, the header says so: memory still shows answers that had a credit line.
    off = rt.search_credit_header(False)
    assert off.startswith("Cognee memory search results") and rt.CREDITS_OFF_NOTE in off
    assert rt.LOGOTYPE not in off


def test_cursor_save_receipt_header(suite, rt):
    if suite.name != "cursor":
        pytest.skip("only cursor ends a save with a receipt")
    header = rt.save_receipt_header(True, "agent_sessions")
    assert "\n" not in header and f"> {rt.LOGOTYPE} Saved to the agent_sessions dataset:" in header
    off = rt.save_receipt_header(False, "agent_sessions")
    assert rt.CREDITS_OFF_NOTE in off and rt.LOGOTYPE not in off


def test_cursor_save_receipt_is_not_a_memory_credit(suite, rt):
    if suite.name != "cursor":
        pytest.skip("only cursor ends a save with a receipt")
    receipt = f"Saved.\n> {rt.LOGOTYPE} Saved to the agent_sessions dataset: Yuno likes Yukki."
    assert rt.check_credits(receipt, True)["credited"] is False


def test_cursor_strip_credit_cuts_receipts_headers_and_the_off_note(suite, rt):
    if suite.name != "cursor":
        pytest.skip("only cursor strips credit text before storing")
    receipt = f"Saved.\n\n> {rt.LOGOTYPE} Saved to the agent_sessions dataset: Yuno likes Yukki."
    assert rt.strip_credit(receipt) == "Saved."
    for header in (
        rt.search_credit_header(True, "agent_sessions"),
        rt.search_credit_header(False),
        rt.save_receipt_header(True, "agent_sessions"),
        rt.save_receipt_header(False),
    ):
        printed = f"NLINES 2\nLINE0 {header}\nlist len 1\n"
        assert rt.strip_credit(printed) == "NLINES 2\nLINE0 list len 1"
        # An old trace holds the printed output JSON-escaped.
        stored = rt.strip_credit(json.dumps({"output": printed}))
        assert "Cognee memory search results" not in stored and "Saved to Cognee" not in stored
        assert "list len 1" in stored


def test_cursor_strip_credit_cuts_credits_inside_escaped_tool_output(suite, rt):
    if suite.name != "cursor":
        pytest.skip("only cursor strips credit text before storing")
    trace = (
        'Bash succeeded. Output: {"stdout": "m4 Cursor session | Answer: Mika likes Yuji. '
        "> \U0001f9e0 Cognee memory [m6]: Mika likes Yuji, from the knowledge graph\\n"
        f'next line > {rt.LOGOTYPE} Mika likes Yuji, based on the agent_sessions dataset.\\n"}}'
    )
    stored = rt.strip_credit(trace)
    assert "Cognee memory [m6]" not in stored and "cognee.ai)" not in stored
    assert "Answer: Mika likes Yuji." in stored and "next line" in stored


def test_cursor_strip_credit_deep(suite, rt):
    if suite.name != "cursor":
        pytest.skip("only cursor strips credit text before storing")
    header = rt.search_credit_header(True, "agent_sessions")
    value = {"command": "cat out.txt", "output": [f"{header}\n[{{}}]", 3], "ok": True}
    assert rt.strip_credit_deep(value) == {
        "command": "cat out.txt",
        "output": ["[{}]", 3],
        "ok": True,
    }


@pytest.mark.parametrize("key", ["../escape", "..", "", "a/b"])
def test_unsafe_session_keys_write_nothing(rt, tmp_path, key):
    state = tmp_path / "state"
    rt.begin_turn(state, key)
    rt.mark_injected(state, key)
    rt.record_credit(state, key, {"injected": True, "credited": True})
    assert not rt.injected_this_turn(state, key)
    assert not any(tmp_path.rglob("*.json"))


# ── the recall hook ────────────────────────────────────────────────────────


@pytest.fixture
def lookup(suite, hook_module, monkeypatch, rt):
    # ``rt`` first: loading a module scrubs COGNEE_* from the environment.
    if suite.name not in ("claude-code", "codex"):
        pytest.skip(f"{suite.name}: recall hook does not credit memory")
    module = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(module, "get_session_key", lambda: SESSION_KEY)
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "on")  # Codex starts with it off
    return module


def _context(run) -> str:
    return run.output["hookSpecificOutput"]["additionalContext"]


def _state_dir(lookup):
    return lookup._plugin_common._PLUGIN_DIR


def _injected(rt, lookup) -> bool:
    return rt.injected_this_turn(_state_dir(lookup), SESSION_KEY)


#: The credit line the instruction asks for: Claude Code's "> Cognee memory:",
#: Codex's braille rows.
CREDIT_LINE_RE = r"> (?:Cognee memory:|`⡏⠁⡏⡇⡏⡁⡧⣸⢸⠅⡯`  <what you remembered>,)"


def test_hit_is_marked_and_carries_the_first_credit_instruction(lookup, monkeypatch, rt):
    ctx = _context(drive_recall(lookup, monkeypatch, recall=HIT))
    # No dataset from the server: every item keeps its origin.
    assert f"[Codex session]\n{PASSAGE_CODEX}" in ctx
    assert f"[Claude Code session learning, 2026-10-03]\n{PASSAGE_LEARNING}" in ctx
    assert "## Relevant entities [knowledge graph entities]" in ctx
    assert "Cognee found 5 memories for this prompt" in ctx
    assert re.search(CREDIT_LINE_RE, ctx)
    assert "first Cognee credit in this session" in ctx
    assert "[m1" not in ctx
    assert _injected(rt, lookup)


def test_same_dataset_results_count_every_item(lookup, monkeypatch):
    """Two results from one dataset share one mark; each passage and block still
    counts once, a later block appended to a section as well."""
    ctx = _context(drive_recall(lookup, monkeypatch, recall=TWO_RESULTS))
    assert "Cognee found 5 memories for this prompt" in ctx
    assert "[dataset ds1]\n### order" in ctx  # the later entities block
    assert f"[dataset ds1]\n{NAMED}" in ctx


def test_marks_name_the_dataset_and_no_document(lookup, monkeypatch):
    ctx = _context(drive_recall(lookup, monkeypatch, recall=TWO_RESULTS))
    assert "[dataset ds1]" in ctx
    assert "document" not in ctx.replace("<document>", "")


def test_a_block_dropped_by_the_budget_is_not_counted(suite, lookup, monkeypatch):
    """A block whose only entry cannot fit is dropped; the passage from the same
    dataset, whose mark is the same, survives and does not make it count."""
    if suite.name != "claude-code":
        pytest.skip("only Claude Code fits memory to a size budget")
    huge = "- " + "the order was returned " * 600
    recall = {
        "graph": [
            TWO_RESULTS["graph"][0],
            _graph(
                "ds1",
                f"## Relevant entities\n### order\n- the order shipped.\n\n"
                f"## Related facts\n{huge}",
            ),
        ]
    }
    ctx = _context(drive_recall(lookup, monkeypatch, recall=recall))
    assert huge not in ctx and f"[dataset ds1]\n{NAMED}" in ctx
    assert "[dataset ds1]\n### order" in ctx  # the later entities block is counted
    assert "Cognee found 4 memories for this prompt" in ctx


def test_nothing_injected_has_no_instruction(suite, lookup, monkeypatch, rt):
    if suite.name != "claude-code":
        pytest.skip("only Claude Code fits memory to a size budget")
    huge = "Cognee doc: " + "detail " * 2000
    recall = {"graph": [_graph("ds1", f"## Relevant passages\n{huge}")]}
    ctx = _context(drive_recall(lookup, monkeypatch, recall=recall))
    assert "Crediting Cognee memory" not in ctx and huge not in ctx
    assert not _injected(rt, lookup)


def test_an_earlier_session_turn_is_marked_with_its_origin(lookup, monkeypatch):
    turn = {"source": "session", "qa_id": "qa-7", "question": "q", "answer": "a"}
    ctx = _context(drive_recall(lookup, monkeypatch, recall={"session_context": [turn]}))
    assert "[earlier session turn]" in ctx
    assert "Cognee found 1 memory for this prompt" in ctx


def test_after_a_credit_the_short_instruction(lookup, monkeypatch, rt):
    rt.record_credit(_state_dir(lookup), SESSION_KEY, {"injected": True, "credited": True})
    ctx = _context(drive_recall(lookup, monkeypatch, recall=HIT))
    assert re.search(CREDIT_LINE_RE, ctx)
    assert "first Cognee credit" not in ctx


def test_empty_recall_has_no_instruction_and_is_no_injected_turn(lookup, monkeypatch, rt):
    drive_recall(lookup, monkeypatch, recall=HIT)
    run = drive_recall(lookup, monkeypatch, recall={})
    ctx = _context(run)
    assert "Crediting Cognee memory" not in ctx
    assert run.message()
    assert not _injected(rt, lookup)


def test_a_recall_that_returns_early_still_starts_a_new_turn(lookup, monkeypatch, rt):
    """Server down, no session or a timeout: the hook returns before marking, and
    the previous turn's memory must not pass for this turn's."""
    import asyncio

    drive_recall(lookup, monkeypatch, recall=HIT)
    assert _injected(rt, lookup)
    monkeypatch.setattr(lookup, "_load_session_id", lambda: "")
    assert asyncio.run(lookup._run("next prompt")) is None
    assert not _injected(rt, lookup)


def test_opt_out(lookup, monkeypatch, rt):
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "off")
    ctx = _context(drive_recall(lookup, monkeypatch, recall=HIT))
    assert "[Codex session]" not in ctx and "Crediting Cognee memory" not in ctx
    assert PASSAGE_CODEX in ctx  # memory itself is unchanged
    assert not _injected(rt, lookup)


def test_default_per_host(suite, lookup, monkeypatch):
    """On by default in Claude Code; off in Codex, whose terminal shows the injected
    instruction, until it has been tried live."""
    monkeypatch.delenv("COGNEE_PLUGIN_CITE_MEMORY")
    ctx = _context(drive_recall(lookup, monkeypatch, recall=HIT))
    assert ("Crediting Cognee memory" in ctx) is suite.cites_memory_by_default


# ── the Stop hook ──────────────────────────────────────────────────────────


@pytest.fixture
def stop(suite, hook_module, monkeypatch, rt):
    if suite.name not in ("claude-code", "codex", "cursor"):
        pytest.skip(f"{suite.name}: no memory credit check")
    module = hook_module(suite, "store-to-session.py")
    events: list = []
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "on")
    monkeypatch.setattr(module, "get_session_key", lambda: SESSION_KEY)
    monkeypatch.setattr(module, "hook_log", lambda e, d=None: events.append((e, d or {})))
    module.events = events
    return module


def test_codex_stop_prints_the_turn_status_under_the_answer(suite, stop, rt, capsys, monkeypatch):
    """Codex prints the recall hook's status after the answer and its credit,
    from the Stop hook, once."""
    if suite.name != "codex":
        pytest.skip("only Codex prints the turn's status from the Stop hook")
    import json as _json

    import _plugin_common
    import cognee_statusline_render

    summary = "Cognee memory: 3 memory hits"
    rt.stash_turn_status(_plugin_common._PLUGIN_DIR, SESSION_KEY, summary)
    # Drawn when printed: a dataset switched during the turn shows as it is now.
    monkeypatch.setattr(
        cognee_statusline_render,
        "render_status_for_host",
        lambda key, auto=False: "● cognee: switched · local",
    )
    stop._print_turn_status()
    printed = _json.loads(capsys.readouterr().out)
    assert printed == {"systemMessage": f"● cognee: switched · local\n{summary}"}
    stop._print_turn_status()  # printed once: a later turn never repeats it
    assert capsys.readouterr().out == ""


def _credit_line(suite) -> str:
    if suite.name == "cursor":
        return "> [​](https://www.cognee.ai) you chose the server."
    return "> Cognee memory: you chose the server, from the ds1 dataset"


def _turn(rt, stop, answer, injected=True):
    import _plugin_common

    state = _plugin_common._PLUGIN_DIR
    rt.begin_turn(state, SESSION_KEY)
    if injected:
        rt.mark_injected(state, SESSION_KEY)
    stop._check_memory_credit({"last_assistant_message": answer})
    return state


def test_stop_records_a_credit_on_an_injected_turn(suite, stop, rt):
    state = _turn(rt, stop, f"ok\n{_credit_line(suite)}")
    assert stop.events == [("memory_credit", {"injected": True, "credited": True})]
    assert rt.has_credited(state, SESSION_KEY)


def test_stop_does_not_count_a_credit_on_a_turn_without_memory(suite, stop, rt):
    state = _turn(rt, stop, f"ok\n{_credit_line(suite)}", injected=False)
    assert stop.events == [("memory_credit", {"injected": False, "credited": True})]
    assert not rt.has_credited(state, SESSION_KEY)


def test_stop_without_credit_logs_an_uncredited_turn(stop, rt):
    state = _turn(rt, stop, "a plain answer")
    assert stop.events == [("memory_credit", {"injected": True, "credited": False})]
    assert not rt.has_credited(state, SESSION_KEY)


def test_stop_is_silent_when_nothing_was_injected_or_credited(stop, rt):
    _turn(rt, stop, "a plain answer", injected=False)
    assert stop.events == []


def test_instruction_comes_before_the_memory(lookup, monkeypatch):
    """Claude Code shows the model only a 2 KB preview of hook context over 10,000
    characters, so the instruction must come before the memory, not after it."""
    big = {"graph": [{"source": "graph", "text": CONTEXT + "\n- filler fact." * 1500}]}
    ctx = _context(drive_recall(lookup, monkeypatch, recall=big))
    at = re.search(CREDIT_LINE_RE, ctx).start()
    assert at < ctx.index("=== Cognee memory")
    assert ctx.index("first Cognee credit") < 2_000


def test_memory_trimmed_to_fit_is_not_counted(lookup, monkeypatch):
    """A host that trims memory to a size budget marks before trimming: only the
    items that made it into the context count as this prompt's."""
    monkeypatch.setenv("COGNEE_INJECT_MAX_CHARS", "1024")
    passages = [f"Cognee doc {i}: " + "detail " * 40 for i in range(12)]
    text = "## Relevant passages\n" + "\n---\n".join(passages)
    ctx = _context(drive_recall(lookup, monkeypatch, recall={"graph": [_graph("ds1", text)]}))
    shown = sum(f"[dataset ds1]\n{p}" in ctx for p in passages)
    if "Crediting Cognee memory" in ctx:  # a tiny size budget can leave it out
        assert f"Cognee found {_noun(shown)} for this prompt" in ctx


# ── "this conversation was saved" on resume ────────────────────────────────

NOW = 1_791_820_000.0  # a fixed "now"; earlier times below are the same day


def test_notice_for_a_final_save(rt):
    state = {"last_improved_at": NOW - 600, "trigger": "final", "improved_dataset": "notes"}
    message, ts = rt.sync_notice(state, now=NOW)
    assert message.startswith(
        "This conversation was sent to the notes dataset in Cognee's long-term memory "
        "when you left at "
    )
    assert ts == NOW - 600


def test_notice_names_the_dataset_of_an_older_record(rt):
    # Records written before improved_dataset carry the save's dataset in ``dataset``.
    state = {"last_improved_at": NOW - 600, "trigger": "final", "dataset": "agent_sessions"}
    message, _ = rt.sync_notice(state, now=NOW)
    assert "sent to the agent_sessions dataset in Cognee's long-term memory" in message


def test_notice_without_a_dataset_still_reads(rt):
    message, _ = rt.sync_notice({"last_improved_at": NOW - 600, "trigger": "final"}, now=NOW)
    assert message.startswith("This conversation was sent to Cognee's long-term memory when ")


def test_notice_for_an_idle_save(rt):
    message, _ = rt.sync_notice({"last_improved_at": NOW - 600, "trigger": "idle"}, now=NOW)
    assert "long-term memory at " in message and "when you left" not in message


def test_notice_for_a_failed_final_save_mentions_the_earlier_one(rt):
    state = {
        "last_improved_at": NOW - 3600,
        "trigger": "idle",
        "last_failed_at": NOW - 600,
        "last_failure_reason": "unreachable",
        "last_failure_trigger": "final",
    }
    message, ts = rt.sync_notice(state, now=NOW)
    assert message.startswith("This conversation wasn't saved to Cognee when you left at ")
    assert "the Cognee server was unreachable" in message
    assert "An earlier save at " in message
    assert ts == NOW - 600


def test_failed_save_names_its_dataset_and_the_earlier_one(rt):
    state = {
        "dataset": "work",
        "improved_dataset": "notes",
        "last_improved_at": NOW - 3600,
        "trigger": "idle",
        "last_failed_at": NOW - 600,
        "last_failure_reason": "unreachable",
        "last_failure_trigger": "final",
    }
    message, _ = rt.sync_notice(state, now=NOW)
    assert message.startswith("This conversation wasn't saved to the work dataset when you left")
    assert "An earlier save to the notes dataset at " in message


def test_a_failed_save_keeps_the_dataset_the_earlier_save_went_to(suite, isolated_modules):
    pc = isolated_modules(suite, "_plugin_common")
    if not hasattr(pc, "record_improve_failure"):
        pytest.skip(f"{suite.name}: records no failed save")
    pc.record_improve_success("s-ds", "notes", "idle")
    pc.record_improve_failure("s-ds", "work", "final", "unreachable")
    state = pc.read_improve_state("s-ds")
    assert (state["dataset"], state["improved_dataset"]) == ("work", "notes")


def test_no_record_no_notice(rt):
    assert rt.sync_notice({}, now=NOW) == ("", 0.0)
    assert rt.sync_notice({"last_improved_at": "junk"}, now=NOW) == ("", 0.0)


def test_another_day_shows_the_date(rt):
    message, _ = rt.sync_notice({"last_improved_at": NOW - 3 * 86400, "trigger": "final"}, now=NOW)
    assert any(
        month in message
        for month in (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        )
    )


def test_notice_is_shown_once_per_save(rt, tmp_path):
    assert rt.notice_due(tmp_path, "claude_s1", 100.0)
    rt.mark_notice_shown(tmp_path, "claude_s1", 100.0)
    assert not rt.notice_due(tmp_path, "claude_s1", 100.0)
    assert rt.notice_due(tmp_path, "claude_s1", 200.0)  # a later save is news again
    assert not rt.notice_due(tmp_path, "", 100.0)


@pytest.fixture
def start(suite, hook_module, monkeypatch):
    if suite.name not in ("claude-code", "codex"):
        pytest.skip(f"{suite.name}: no resume notice")
    module = hook_module(suite, "session-start.py")
    import config

    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "on")
    monkeypatch.setattr(config, "get_session_id", lambda *a, **k: "claude_resumed")
    return module


RESUME = {"source": "resume"}


def test_resume_shows_the_notice_once(start):
    import _plugin_common

    _plugin_common.record_improve_success("claude_resumed", "agent_sessions", "final")
    out = start._apply_sync_notice(
        {"hookSpecificOutput": {"hookEventName": "SessionStart"}}, RESUME
    )
    # Top level is what Claude Code shows the user; additionalContext reaches the agent.
    assert out["systemMessage"].startswith(
        "This conversation was sent to the agent_sessions dataset in Cognee's long-term memory"
    )
    assert "🧠" not in out["systemMessage"]
    assert out["hookSpecificOutput"]["additionalContext"] == out["systemMessage"]
    again = start._apply_sync_notice({}, RESUME)
    assert again == {}


@pytest.mark.parametrize("source", ["compact", "startup", "clear", ""])
def test_only_a_resume_shows_the_notice(start, source):
    """A compaction keeps the session too, but lands mid-task."""
    import _plugin_common

    _plugin_common.record_improve_success("claude_resumed", "agent_sessions", "idle")
    assert start._apply_sync_notice({}, {"source": source}) == {}
    assert start._apply_sync_notice({}, RESUME)["systemMessage"]  # still due


def test_a_new_chat_shows_nothing(start):
    assert start._apply_sync_notice({}, RESUME) == {}


# ── the trace store ────────────────────────────────────────────────────────


def test_cursor_trace_is_stored_without_credit_text(suite, hook_module, monkeypatch, rt):
    """An agent that prints a search's output stores the credit instruction in that
    trace; stored, it comes back in later recalls as a rule, even with credits off."""
    import asyncio

    if suite.name != "cursor":
        pytest.skip("only cursor strips credit text from traces")
    module = hook_module(suite, "store-to-session.py")
    sent: list = []
    monkeypatch.setattr(module, "_load_session", lambda: ("cursor_s1", "agent_sessions", "u1"))
    monkeypatch.setattr(module, "server_usable", lambda *_: True)
    monkeypatch.setattr(module, "resolve_runtime_mode", lambda: {"base_url": "http://x"})
    monkeypatch.setattr(
        module, "remember_entry_via_http", lambda dataset, sid, entry: sent.append(entry) or {}
    )
    header = rt.search_credit_header(True, "agent_sessions")
    asyncio.run(
        module._store_tool_call(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "python3 -c 'print(open(\"out.txt\").read())'"},
                "tool_output": f'{header}\n[{{"text": "Mika likes Yuji."}}]',
            }
        )
    )
    assert len(sent) == 1
    stored = json.dumps(sent[0], ensure_ascii=False)
    assert "Cognee memory search results" not in stored and "cognee.ai)" not in stored
    assert "Mika likes Yuji." in stored


def test_codex_reports_whether_the_icon_was_copied_exactly(suite, rt):
    if suite.name != "codex":
        pytest.skip("only Codex draws the icon in KaTeX")
    icon = f"> {rt.KATEX_ICON}{rt.ICON_PADDING}"
    exact = f"Alice is curious.\n\n{icon}Alice's journey,\\\n> from a dataset"
    result = rt.check_credits(exact, True)
    assert result["credited"] and result["logo_exact"] is True
    mangled = exact.replace(r"\kern{0.1em}", r"\kern{0.12em}", 1)
    result = rt.check_credits(mangled, True)
    assert result["credited"] and result["logo_exact"] is False
    braille_credit = "ok\n> `⡏⠁⡏⡇⡏⡁⡧⣸⢸⠅⡯`  x,\n> `⠉⠁⠉⠁⠉⠁⠁⠈⠈⠁⠉`  from a"
    braille = rt.check_credits(braille_credit, True)
    assert "logo_exact" not in braille
