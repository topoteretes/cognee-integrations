"""Conformance: the hook plugins' capture policy matches the shared table.

``integrations/conformance/capture_policy_cases.json`` pins which tool calls
automatic capture keeps (``COGNEE_CAPTURE_TOOLS`` / ``_DENY_TOOLS`` /
``_DENY_PATHS``) and what redaction does to a captured value. openclaw's
TypeScript port reads the same file, so the two cannot drift apart.

``expected: "error"`` means the policy raises: a malformed pattern fails closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from utils.suites import ANTIGRAVITY, CLAUDE, CODEX

_PLUGINS = [CLAUDE, CODEX, ANTIGRAVITY]
_ENV = (
    "COGNEE_CAPTURE",
    "COGNEE_CAPTURE_TOOLS",
    "COGNEE_CAPTURE_DENY_TOOLS",
    "COGNEE_CAPTURE_DENY_PATHS",
    "COGNEE_CAPTURE_REDACT",
    "COGNEE_CAPTURE_REDACT_PATTERNS",
)


def _load_cases() -> dict:
    integrations = next(p for p in Path(__file__).resolve().parents if p.name == "integrations")
    cases = integrations / "conformance" / "capture_policy_cases.json"
    return json.loads(cases.read_text(encoding="utf-8"))


def _set_env(monkeypatch, env: dict) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("plugin_suite", _PLUGINS, ids=lambda s: s.name)
def test_tool_decisions_match_shared_table(plugin_suite, isolated_modules, monkeypatch):
    policy = isolated_modules(plugin_suite, "_capture_policy")

    mismatches = []
    for case in _load_cases()["tool_cases"]:
        _set_env(monkeypatch, case["env"])
        try:
            result = policy.allow_tool(case["tool"], case["params"])
        except Exception:
            result = "error"
        if result != case["expected"]:
            mismatches.append(f"{case['note']}: {result!r}, expected {case['expected']!r}")

    assert not mismatches, "capture tool policy drift:\n" + "\n".join(mismatches)


@pytest.mark.parametrize("plugin_suite", _PLUGINS, ids=lambda s: s.name)
def test_invalid_patterns_are_reported(plugin_suite, isolated_modules, monkeypatch):
    policy = isolated_modules(plugin_suite, "_capture_policy")

    for case in _load_cases()["tool_cases"]:
        if case["expected"] != "error" or "COGNEE_CAPTURE_DENY_PATHS" in case["env"]:
            continue  # deny paths are parsed lazily and not part of the notice
        _set_env(monkeypatch, case["env"])
        assert policy.capture_pattern_error(), case["note"]


@pytest.mark.parametrize("plugin_suite", _PLUGINS, ids=lambda s: s.name)
def test_redaction_matches_shared_table(plugin_suite, isolated_modules, monkeypatch):
    policy = isolated_modules(plugin_suite, "_capture_policy")

    mismatches = []
    for case in _load_cases()["redact_cases"]:
        _set_env(monkeypatch, case["env"])
        if case.get("error"):
            try:
                policy.redact(case["input"])
            except Exception:
                continue
            mismatches.append(f"{case['note']}: did not raise")
            continue
        result = policy.redact(case["input"])
        if result != case["expected"]:
            mismatches.append(f"{case['note']}: {result!r}, expected {case['expected']!r}")

    assert not mismatches, "capture redaction drift:\n" + "\n".join(mismatches)
