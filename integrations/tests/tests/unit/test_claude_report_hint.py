"""Evaluation routing stays distinct from activity summaries and fits the hook budget."""

import json

import pytest
from utils.recall import drive_recall, load_lookup


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("evaluate cognee", True),
        ("how is cognee performing", True),
        ("is cognee worth it", True),
        ("did the update help", True),
        ("assess Cognee", True),
        ("audit Cognee", True),
        ("Cognee stats", True),
        ("memory quality", True),
        ("give me a standup", False),
        ("weekly digest", False),
    ],
)
def test_evaluation_hint_routing(suite, hook_module, monkeypatch, prompt, expected):
    if suite.name != "claude-code":
        pytest.skip("Claude report hint")
    lookup = load_lookup(suite, hook_module, monkeypatch)
    assert bool(lookup._report_hint(prompt)) is expected
    monkeypatch.setenv("COGNEE_REPORT_HINT", "false")
    assert lookup._report_hint(prompt) == ""


def test_evaluation_hint_is_one_line_inside_budget(suite, hook_module, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("Claude report hint")
    lookup = load_lookup(suite, hook_module, monkeypatch)
    result = drive_recall(lookup, monkeypatch, prompt="evaluate cognee", recall={})
    context = result.output["hookSpecificOutput"]["additionalContext"]
    hint = "Use /cognee-report for an evaluation"
    assert context.count(hint) == 1
    assert len(json.dumps(result.output)) + 1 <= lookup.injection_limit()
    monkeypatch.setenv("COGNEE_REPORT_HINT", "false")
    plain = drive_recall(lookup, monkeypatch, prompt="evaluate cognee", recall={})
    assert hint not in plain.output["hookSpecificOutput"]["additionalContext"]
    assert (
        result.output["hookSpecificOutput"]["systemMessage"]
        == plain.output["hookSpecificOutput"]["systemMessage"]
    )
