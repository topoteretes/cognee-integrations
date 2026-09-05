"""Validate the current registry and compatibility of all structured loggers."""

import ast
import json
from pathlib import Path


def test_current_events_registered(suite, isolated_modules):
    events = isolated_modules(suite, "event_names")
    root = Path(events.__file__).parent
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in ("hook_log", "_config_log", "_log") or not node.args:
                continue
            if not isinstance(node.args[0], ast.Constant):
                continue
            source = (
                path.stem
                if node.func.id == "_log"
                else ("config" if node.func.id == "_config_log" else "hook")
            )
            key = source + ":" + node.args[0].value
            assert key in events.EVENT_NAMES, (path, key)
    doc = (root.parent / "EVENTS.md").read_text(encoding="utf-8")
    for canonical in events.EVENT_NAMES.values():
        assert "`" + canonical + "`" in doc


def test_legacy_and_canonical_readers(suite, isolated_modules, monkeypatch, tmp_path):
    common = isolated_modules(suite, "_plugin_common")
    log = tmp_path / "events.log"
    monkeypatch.setattr(common, "_HOOK_LOG", log)
    common.hook_log("context_lookup_hit", {"count": 1})
    first = json.loads(log.read_text())
    assert first["event"] == "context_lookup_hit"
    assert first["event_name"] == "recall.lookup_hit"
    monkeypatch.setenv("COGNEE_LOG_EVENT_SCHEMA", "2")
    common.hook_log("context_lookup_hit")
    second = json.loads(log.read_text().splitlines()[-1])
    assert second["event"] == first["event_name"]
    assert second["legacy_event"] == first["event"]
