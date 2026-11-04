"""Conformance: the hook plugins' dataset-name sanitizer matches the shared spec.

``integrations/conformance/dataset_name_cases.json`` is the single source of truth
for the rule every integration applies to a dataset name: cognee rejects names
containing a space or a dot (``check_dataset_name``, run on every write), so
those — and only those — are rewritten. Each integration checks its own
sanitizer against the one table.

Beyond the table: a configured ``COGNEE_PLUGIN_DATASET`` is sanitized when the
config loads, an explicit switch to an invalid name is refused rather than
rewritten, and code-graph dataset names never carry a dot.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from utils.suites import ANTIGRAVITY, CLAUDE, CODEX

_PLUGINS = [CLAUDE, CODEX, ANTIGRAVITY]


def _load_cases() -> list[dict]:
    integrations = next(p for p in Path(__file__).resolve().parents if p.name == "integrations")
    cases = integrations / "conformance" / "dataset_name_cases.json"
    return json.loads(cases.read_text(encoding="utf-8"))


@pytest.mark.parametrize("plugin_suite", _PLUGINS, ids=lambda s: s.name)
def test_sanitizer_matches_shared_table(plugin_suite, isolated_modules):
    config = isolated_modules(plugin_suite, "config")

    mismatches = []
    for case in _load_cases():
        result = config.sanitize_dataset_name(case["input"], case["fallback"])
        if result != case["expected"]:
            mismatches.append(f"{case['input']!r} -> {result!r}, expected {case['expected']!r}")

    assert not mismatches, "dataset-name sanitization drift:\n" + "\n".join(mismatches)


@pytest.mark.parametrize("plugin_suite", _PLUGINS, ids=lambda s: s.name)
def test_configured_dataset_is_sanitized_and_logged(plugin_suite, isolated_modules, monkeypatch):
    config = isolated_modules(plugin_suite, "config")
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        config, "_config_log", lambda event, detail=None: events.append((event, detail))
    )

    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "my project.v2")
    assert config.load_config()["dataset"] == "my_project_v2"
    assert events == [("dataset_name_sanitized", {"from": "my project.v2", "to": "my_project_v2"})]

    events.clear()
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "Foo+Bar")
    assert config.load_config()["dataset"] == "Foo+Bar"
    assert events == []


@pytest.mark.parametrize("plugin_suite", _PLUGINS, ids=lambda s: s.name)
def test_switch_refuses_an_invalid_name_with_a_suggestion(plugin_suite, hook_module):
    switch = hook_module(plugin_suite, "switch-dataset.py")
    with pytest.raises(
        switch.SwitchError, match=r"cognee rejects spaces and dots \(try 'my_data'\)"
    ):
        switch._switch("host-1", {}, "my.data", force=False)
    with pytest.raises(switch.SwitchError, match="cognee rejects spaces and dots$"):
        switch._switch("host-1", {}, "...", force=False)


@pytest.mark.parametrize("plugin_suite", _PLUGINS, ids=lambda s: s.name)
def test_code_graph_dataset_name_has_no_dot(plugin_suite, isolated_modules, tmp_path):
    code_graph = isolated_modules(plugin_suite, "_code_graph")
    repo = tmp_path / "Foo.JS"
    repo.mkdir()
    name = code_graph.default_code_dataset(str(repo))
    assert name.startswith("codebase-foo-js-")
    assert "." not in name
