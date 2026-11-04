"""``recall-trim.py``: the hooks module's search shows what the prompt hook injects.

The module's ``search`` tool and the memory pane's search box run
``cognee-search.sh`` and pipe its output through this script, so a graph item
(on cognee >= 1.6.0 the whole prompt the server's LLM would have read) is cut
down to its retrieved context exactly as ``session-context-lookup.py`` cuts it,
and a code item's facts are rendered one per line. Anything that is not a JSON
list passes through unchanged: the module's reading of an error envelope or the
UNREACHABLE sentinel must not change because the trimmer sat in between.
"""

from __future__ import annotations

import json

import pytest

QUERY = "how does tempo replicate?"
PASSAGE = "Project tempo replicates with raft."
ONLY_CONTEXT = (
    "Previous conversation:\n\n[2026-10-13] QUESTION: hi\nANSWER: hello\n\n"
    f"The question is: `{QUERY}`\n\nUse the context: `## Relevant passages\n{PASSAGE}`"
)


@pytest.fixture(autouse=True)
def _only_the_suite_with_the_module(suite):
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: no hooks module, so no recall-trim.py")


def _trim(run_hook, suite, stdin: str, *args: str):
    result = run_hook(suite, "recall-trim.py", *args, stdin=stdin)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_graph_item_is_cut_to_its_retrieved_context(suite, run_hook):
    hits = [{"source": "graph", "text": ONLY_CONTEXT}]
    out = json.loads(_trim(run_hook, suite, json.dumps(hits), "--query", QUERY))
    assert len(out) == 1
    assert out[0]["source"] == "graph"
    assert PASSAGE in out[0]["text"]
    assert "Previous conversation" not in out[0]["text"]
    assert "The question is" not in out[0]["text"]


def test_a_code_item_is_rendered_one_fact_per_line(suite, run_hook):
    facts = {
        "facts": [{"name": "process_payment", "kind": "function", "file": "pay.py", "line": 12}]
    }
    hits = [{"source": "code", "text": json.dumps(facts)}]
    out = json.loads(_trim(run_hook, suite, json.dumps(hits), "--query", QUERY))
    assert "process_payment" in out[0]["text"]
    assert not out[0]["text"].lstrip().startswith("{"), out[0]["text"]


def test_text_the_trimmer_cannot_parse_passes_through_whole(suite, run_hook):
    hits = [{"source": "graph", "text": "bare context from an older server"}]
    out = json.loads(_trim(run_hook, suite, json.dumps(hits), "--query", QUERY))
    assert out == hits


@pytest.mark.parametrize(
    "raw",
    ["UNREACHABLE\n", json.dumps({"error": "boom", "code": 1}) + "\n", "not json at all\n", ""],
)
def test_anything_but_a_list_passes_through_unchanged(suite, run_hook, raw):
    assert _trim(run_hook, suite, raw, "--query", QUERY) == raw
