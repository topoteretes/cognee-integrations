"""Whole-entry budgeting for Claude Code hooks."""

from __future__ import annotations

import importlib.util
import json
from functools import partial
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "claude-code/scripts/_injection_budget.py"
spec = importlib.util.spec_from_file_location("claude_injection_budget", SCRIPT)
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


def test_thirty_large_passages_keep_header_and_whole_entries():
    passages = [f"passage {i}\n```python\n" + "x" * 1100 + "\n```" for i in range(30)]
    text, stats = budget.fit_sections(
        [
            {"entries": ["Cognee memory: 30 hits"], "required": True},
            {"title": "=== Memory ===", "entries": passages, "priority": 40},
        ],
        9000,
    )
    assert len(text) <= 9000
    assert text.startswith("Cognee memory: 30 hits")
    assert 0 < stats["entries_kept"] < 30
    assert stats["entries_dropped"] + stats["entries_kept"] == 30
    assert text.count("```") == stats["entries_kept"] * 2
    assert all(entry in passages for entry in stats["entries"])


def test_priority_and_empty_headings():
    text, stats = budget.fit_sections(
        [
            {"entries": ["header"], "required": True},
            {"title": "code", "entries": ["code fact"], "priority": 50},
            {"title": "trace", "entries": ["trace " * 1000], "priority": 10},
        ],
        50,
    )
    assert text == "header\n\ncode\ncode fact"
    assert stats["entries_dropped"] == 1


def test_header_survives_entry_larger_than_budget():
    text, stats = budget.fit_sections(
        [
            {"entries": ["header"], "required": True},
            {"entries": ["x" * 20000]},
        ],
        9000,
    )
    assert text == "header"
    assert stats["entries_kept"] == 0


def test_required_header_larger_than_budget_is_explicit_error():
    with pytest.raises(ValueError, match="header"):
        budget.fit_sections([{"entries": ["header"], "required": True}], 1)


@pytest.mark.parametrize(
    "value,expected", [("256", 1024), ("999999", 9000), ("5000", 5000), ("bad", 9000)]
)
def test_limit_never_exceeds_host_ceiling(monkeypatch, value, expected):
    monkeypatch.setenv("COGNEE_INJECT_MAX_CHARS", value)
    assert budget.injection_limit() == expected


def test_serialized_unicode_and_backslashes_stay_bounded():
    text, _ = budget.fit_sections(
        [
            {"entries": ["header"], "required": True},
            {"entries": ["🧠" * 300 + "\\" * 100] * 30},
        ],
        9000,
    )
    assert len(json.dumps(text)) - 2 <= 9000


@pytest.fixture
def lookup(suite, hook_module):
    if suite.name not in {"claude-code", "codex"}:
        pytest.skip("Claude Code injection policy")
    return hook_module(suite, "session-context-lookup.py")


def test_relations_from_all_datasets_outlive_overflowing_passages(lookup):
    sections = lookup._injection_sections(
        {
            "graph_context": [
                {
                    "text": (
                        "## Relevant passages\n"
                        + f"passage-{i} " * 150
                        + f"\n\n## Relevant entities\nentity-{i}"
                        + f"\n\n## Related facts\nfact-{i}"
                    )
                }
                for i in range(2)
            ]
        }
    )
    text, _ = budget.fit_sections(sections, 500)
    for i in range(2):
        assert f"entity-{i}" in text and f"fact-{i}" in text
        assert f"passage-{i}" not in text


@pytest.mark.parametrize(
    "limit,notice",
    [
        ("256", ""),
        ("9000", "Cognee: base URL changed: " + "https://cloud.example/" * 20),
        ("9000", "Cognee: invalid capture pattern: " + "\\🧠" * 50),
        ("256", "Cognee: base URL changed: " + "https://cloud.example/" * 1000),
    ],
)
def test_final_recall_json_includes_notices_within_budget(
    suite, lookup, monkeypatch, limit, notice
):
    from utils.recall import drive_recall

    monkeypatch.setenv("COGNEE_INJECT_MAX_CHARS", limit)
    monkeypatch.setattr(lookup, "_run", partial(lookup._run, notice=notice))
    body = "## Relevant passages\n" + "\n---\n".join("🧠\\" * 300 for _ in range(30))
    run = drive_recall(lookup, monkeypatch, recall={"graph": [{"source": "graph", "text": body}]})
    rendered = json.dumps(run.output) + "\n"
    assert len(rendered) <= budget.injection_limit()
    context = run.output["hookSpecificOutput"]["additionalContext"]
    assert "Cognee memory:" in context
    if notice and len(notice) < 1000:
        assert notice in context
        assert notice in run.output["systemMessage"]
        if suite.name == "claude-code":
            assert notice in run.output["hookSpecificOutput"]["systemMessage"]
    elif notice:
        assert "configuration notice omitted" in context


def test_main_consumes_notices_once_before_rendering(suite, lookup, monkeypatch, capsys):
    import io
    import sys

    common = sys.modules["_plugin_common"]
    calls = []
    monkeypatch.setattr(
        common, "base_url_change_notice", lambda: calls.append("url") or "URL notice"
    )
    monkeypatch.setattr(
        common, "capture_policy_notice", lambda: calls.append("capture") or "Capture notice"
    )
    monkeypatch.setattr(lookup, "get_session_key", lambda: "session")
    monkeypatch.setattr(lookup, "set_session_key", lambda key: None)

    async def run(prompt, cwd, notice=""):
        assert notice == "URL notice\nCapture notice"
        return lookup._recall_output("Cognee memory: recall 0 memory", notice)

    monkeypatch.setattr(lookup, "_run", run)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"session_id": "session", "prompt": "recall memory"}))
    )
    result = lookup.main()
    output = result if suite.name == "codex" else json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["additionalContext"].startswith(
        "URL notice\nCapture notice"
    )
    assert calls == ["url", "capture"]


@pytest.mark.parametrize(
    "source,heading",
    [
        ("graph", "## Relevant passages\npassage\n\n## Related facts\n"),
        ("graph", ""),
        ("session_context", ""),
        ("code", ""),
    ],
)
def test_large_real_results_retain_whole_facts(lookup, monkeypatch, source, heading):
    from utils.recall import arm_code_lane, drive_recall

    if source == "code":
        arm_code_lane(monkeypatch)
    facts = [f"- fact-{i}: " + "evidence " * 20 for i in range(100)]
    body = heading + "\n".join(facts)
    assert len(body) > 12000
    run = drive_recall(lookup, monkeypatch, recall={source: [{"source": source, "text": body}]})
    context = run.output["hookSpecificOutput"]["additionalContext"]
    assert facts[0] in context
    assert facts[-1] not in context
    assert len(json.dumps(run.output)) + 1 <= 9000
    detail = run.detail("context_lookup_hit")
    assert detail["entries_kept"] > 0 and detail["entries_dropped"] > 0
    if heading:
        assert "## Relevant passages" not in context


def test_fenced_code_is_one_atomic_entry(lookup):
    block = "```python\n" + "first()\n" * 150 + "second()\n```"
    assert lookup._line_entries("fact\n" + block + "\nafter") == ["fact", block, "after"]


@pytest.mark.parametrize("case,expected_hits", [("miss", 0), ("dropped", 0), ("partial", 1)])
def test_status_counts_only_injected_results(suite, lookup, monkeypatch, case, expected_hits):
    from utils.recall import drive_recall

    monkeypatch.setenv("COGNEE_INJECT_MAX_CHARS", "1024")
    monkeypatch.setattr(lookup, "get_session_key", lambda: "review-counts")
    messages = []
    items = [] if case == "miss" else [{"source": "graph", "text": "```\n" + "x" * 12000 + "\n```"}]
    if case == "partial":
        items.append({"source": "graph", "text": "Useful retained fact"})

    def recall(prompt, **kwargs):
        lookup.notify = messages.append
        return items if kwargs["scope"] == ["graph"] else []

    run = drive_recall(lookup, monkeypatch, recall=recall)
    context = run.output["hookSpecificOutput"]["additionalContext"]
    assert f"recall {expected_hits} memory" in context
    marker = json.loads(
        (Path.home() / f".cognee-plugin/{suite.state_subdir}/last_recall.json").read_text()
    )
    assert marker["hits"]["graph_context"] == expected_hits
    assert marker["retrieved_hits"]["graph_context"] == len(items)
    assert marker["session_totals"]["turns_with_hits"] == expected_hits
    if case == "miss":
        assert messages[0].startswith("no recall matches")
    else:
        assert "1 dropped to fit" in context and "1 dropped to fit" in messages[0]
    assert len(json.dumps(run.output)) + 1 <= 1024


def test_many_evictions_measure_the_hook_envelope_only_twice():
    measurements = []

    def measure(text):
        measurements.append(len(text))
        return len(json.dumps({"additionalContext": text, "notice": "🧠\\" * 50})) + 1

    text, stats = budget.fit_sections(
        [
            {"entries": ["Cognee memory"], "required": True},
            {"title": "Facts", "entries": [f"fact-{i}: " + "🧠\\" * 100 for i in range(2000)]},
        ],
        9000,
        measure=measure,
    )
    assert len(measurements) == 2
    assert measurements[0] == len("Cognee memory")
    assert 0 < stats["entries_kept"] < 2000
    assert len(json.dumps({"additionalContext": text, "notice": "🧠\\" * 50})) + 1 <= 9000


def test_graph_sections_render_shared_headings_once(lookup):
    facts = "\n".join(f"- entity-{i} relates to useful concept-{i}" for i in range(60))
    passages = "first passage\n\nstill the first passage\n---\nsecond passage"
    sections = lookup._injection_sections(
        {
            "graph_context": [
                {
                    "text": "Global retrieval context\n\n"
                    f"## Relevant passages\n{passages}\n\n## Related facts\n{facts}"
                }
            ]
        }
    )
    text, stats = budget.fit_sections(sections, 9000)
    assert text.count("## Related facts") == 1
    assert text.count("## Relevant passages") == 1
    assert text.count("=== Cognee memory: facts ===") == 1
    assert text.count("=== Cognee memory: passages ===") == 1
    assert stats["source_hits"]["graph_context"] == 1
    assert all(
        "[cognee-memory]" not in entry for section in sections for entry in section["entries"]
    )
    assert "entity-59 relates to useful concept-59" in text
    assert passages in text
    assert text.index("Global retrieval context") < text.index("## Related facts")
    assert text.index("Global retrieval context") < text.index("## Relevant passages")
