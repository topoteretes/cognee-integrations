"""Conformance: ``_recall_text`` matches the shared recall-trimming table.

``integrations/conformance/recall_text_cases.json`` pins what trimming a cognee
1.6.x ``only_context`` item produces — the retrieved context, the guidance block
and the logged stats — plus the outputs of the passage cap, the entity pruner
and the passage splitter. The table was generated from this module; openclaw's
TypeScript port (``integrations/openclaw/src/recall-text.ts``) checks itself
against the same file in ``__tests__/unit/test_recall_text.ts``, so neither side
can drift unnoticed. Every hook suite ships the module byte-identical, so all
are exercised.
"""

from __future__ import annotations

import json
from pathlib import Path

_KINDS = {"trim", "cap_passage", "prune_entities", "split_passages"}


def _load_cases() -> list[dict]:
    integrations = next(p for p in Path(__file__).resolve().parents if p.name == "integrations")
    cases = integrations / "conformance" / "recall_text_cases.json"
    return json.loads(cases.read_text(encoding="utf-8"))


def _run(rt, case: dict) -> dict:
    kind = case["kind"]
    if kind == "trim":
        context, guidance, stats = rt.trim_recall_text(
            case["input_text"],
            case["prompt"],
            case["session_id"],
            passage_chars=case["passage_chars"],
            context_chars=case["context_chars"],
        )
        return {"context": context, "guidance": guidance, "stats": stats}
    if kind == "cap_passage":
        return {"text": rt.cap_passage(case["input_text"], case["passage_chars"])}
    if kind == "prune_entities":
        text, removed = rt.prune_entities(case["input_text"])
        return {"text": text, "removed": removed}
    passages = rt.split_passages(case["input_text"])
    return {"passages": passages, "session_ids": [rt.passage_session_id(p) for p in passages]}


def test_the_table_covers_every_kind():
    assert {case["kind"] for case in _load_cases()} == _KINDS


def test_recall_text_matches_shared_table(suite, isolated_modules, monkeypatch):
    rt = isolated_modules(suite, "_recall_text")
    # Cases with a null option rely on the table's reference defaults. Suites may
    # ship their own defaults (claude-code budgets the whole hook output instead),
    # so pin the reference values rather than reading each suite's. Set after
    # loading: the loader scrubs every COGNEE_* variable.
    monkeypatch.setenv("COGNEE_RECALL_PASSAGE_CHARS", "2000")
    monkeypatch.setenv("COGNEE_RECALL_CONTEXT_CHARS", "12000")

    mismatches = []
    for case in _load_cases():
        result = _run(rt, case)
        if result != case["expected"]:
            mismatches.append(f"{case['kind']}:{case['name']}: {result!r} != {case['expected']!r}")

    assert not mismatches, "recall-text trimming drift:\n" + "\n".join(mismatches)
