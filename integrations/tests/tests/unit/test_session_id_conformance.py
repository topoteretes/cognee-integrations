"""Conformance: the hook plugins' session-id sanitizer matches the shared spec.

``integrations/conformance/session_id_cases.json`` is the single source of truth
for the rule every integration applies to a native session id before it becomes
a Cognee session id. Each integration checks its own sanitizer against that one
table, so an implementation that drifts fails rather than quietly writing
mismatched session ids into the graph.

claude-code and codex have no pytest project of their own, so their copy of the
check lives here — the shared suite already carries both plugin trees and runs
on every change to either (``detect-changes`` in ci.yml maps them to ``tests``).

antigravity ships the same helper but predates the ASCII guard: it uses a bare
``isalnum()``, so unicode letters and digits survive. It is left out of the
parametrization until its sanitizer is reconciled; adding it here is a one-line
change once it is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from utils.suites import CLAUDE, CODEX


def _load_cases() -> list[dict]:
    integrations = next(p for p in Path(__file__).resolve().parents if p.name == "integrations")
    cases = integrations / "conformance" / "session_id_cases.json"
    return json.loads(cases.read_text(encoding="utf-8"))


@pytest.mark.parametrize("plugin_suite", [CLAUDE, CODEX], ids=lambda s: s.name)
def test_sanitizer_matches_shared_table(plugin_suite, isolated_modules):
    common = isolated_modules(plugin_suite, "_plugin_common")

    mismatches = []
    for case in _load_cases():
        result = common._sanitize_session_key(case["input"])
        if result != case["expected"]:
            mismatches.append(f"{case['input']!r} -> {result!r}, expected {case['expected']!r}")

    assert not mismatches, "session-id sanitization drift:\n" + "\n".join(mismatches)
