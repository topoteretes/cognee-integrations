"""E2e: Cursor's ``cognee-search.sh`` heads graph hits with the credit instruction.

The prompt hook's memory block rides on the turn's first tool result other than
``Read``, which is usually the agent's own search. Cursor saves a large result to
a file and shows the agent only a pointer to it, so the agent answers from the
file, away from the instruction. The search therefore prints the instruction as
its first line, above the untouched results. Code searches, empty results and
``COGNEE_PLUGIN_CITE_MEMORY=off`` print the results alone, with the off note
above graph hits: memory still holds answers that ended with a credit line.
Credit text inside the results (stored by Claude Code sessions, or by Cursor
before stripping existed) is cut on the way out.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from utils.isolation import build_env, usable_bash

BASH = usable_bash()
HITS = [{"source": "graph", "text": "Mika likes Yuji."}]

pytestmark = pytest.mark.skipif(
    BASH is None, reason="requires a working POSIX bash (wrapper is not run on Windows)"
)


@pytest.fixture(autouse=True)
def _only_cursor(suite):
    if suite.name != "cursor":
        pytest.skip(f"{suite.name}: the search prints results only")


def _search(suite, home, mock_server, *args, extra=None):
    env = build_env(suite, home, service_url=mock_server.url, api_key="k", extra=extra)
    result = subprocess.run(
        [BASH, str(suite.scripts_dir / "cognee-search.sh"), "what does mika like", "10", *args],
        env=env,
        cwd=str(home),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_graph_hits_start_with_the_credit_instruction(
    suite, temp_home, mock_server, assert_clean_real_home
):
    mock_server.set_recall_results(HITS)
    header, _, rest = _search(suite, temp_home, mock_server, "--graph").partition("\n")
    assert header.startswith("Cognee memory search results")
    assert "[\u200b](https://www.cognee.ai)" in header
    assert json.loads(rest) == HITS


def test_no_hits_no_instruction(suite, temp_home, mock_server, assert_clean_real_home):
    mock_server.set_recall_results([])
    assert json.loads(_search(suite, temp_home, mock_server, "--graph")) == []


def test_opt_out_says_credits_are_off(suite, temp_home, mock_server, assert_clean_real_home):
    mock_server.set_recall_results(HITS)
    out = _search(
        suite, temp_home, mock_server, "--graph", extra={"COGNEE_PLUGIN_CITE_MEMORY": "off"}
    )
    header, _, rest = out.partition("\n")
    assert header.startswith("Cognee memory search results")
    assert "Cognee credits are off" in header and "cognee.ai)" not in header
    assert json.loads(rest) == HITS


def test_credit_text_in_the_results_is_cut(suite, temp_home, mock_server, assert_clean_real_home):
    stored = (
        "Question: what does mika like?\nAnswer: Mika likes Yuji.\n"
        "> \U0001f9e0 Cognee memory [m6]: Mika likes Yuji, from the knowledge graph.\n"
        "Question: next"
    )
    mock_server.set_recall_results([{"source": "graph", "text": stored}])
    _, _, rest = _search(suite, temp_home, mock_server, "--graph").partition("\n")
    text = json.loads(rest)[0]["text"]
    assert "Cognee memory [m6]" not in text
    assert "Answer: Mika likes Yuji." in text and "Question: next" in text
