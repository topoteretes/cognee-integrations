"""The detached session-end sync must honor the project dataset picker.

The final sync runs in a detached worker with no stdin payload, so it can't
recover the project ``cwd`` itself. ``_spawn_detached_sync(cwd)`` therefore
resolves the picker-aware dataset up front and pins it (plus ``CLAUDE_CWD``) in
the child's environment. Without this, a ``.cognee/session-config.json`` dataset
is honored all session and then silently dropped at the final flush.

Run: python integrations/claude-code/tests/test_sync_picker_propagation.py (or via pytest).
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import config  # noqa: E402


def _load_sync_module():
    spec = importlib.util.spec_from_file_location(
        "sync_session_to_graph", str(_SCRIPTS / "sync-session-to-graph.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    plugin = home / ".cognee-plugin" / "claude-code"
    plugin.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(config, "_CONFIG_DIR", plugin)
    monkeypatch.setattr(config, "_STATE_DIR", plugin)
    monkeypatch.setattr(config, "_CONFIG_FILE", plugin / "config.json")
    monkeypatch.setattr(config, "_HOOK_LOG", plugin / "hook.log")
    for key in list(os.environ.keys()):
        if key.startswith("COGNEE_") or key == "CLAUDE_CWD":
            monkeypatch.delenv(key, raising=False)


def _write_picker(project: Path, data: dict):
    (project / ".cognee").mkdir(parents=True, exist_ok=True)
    (project / ".cognee" / "session-config.json").write_text(json.dumps(data), encoding="utf-8")


def test_detached_sync_pins_picked_dataset(monkeypatch, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _write_picker(project, {"dataset": "picker-dataset"})

    sync = _load_sync_module()
    captured = {}

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            captured["env"] = kwargs.get("env", {})

    monkeypatch.setattr(sync.subprocess, "Popen", _FakePopen)

    assert sync._spawn_detached_sync(str(project)) is True
    assert captured["env"].get("COGNEE_SYNC_DATASET") == "picker-dataset"
    assert captured["env"].get("CLAUDE_CWD") == str(project)


def test_detached_sync_does_not_override_explicit_dataset(monkeypatch, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _write_picker(project, {"dataset": "picker-dataset"})
    # An upstream spawner already pinned a dataset — it must win (setdefault).
    monkeypatch.setenv("COGNEE_SYNC_DATASET", "explicit-dataset")

    sync = _load_sync_module()
    captured = {}

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            captured["env"] = kwargs.get("env", {})

    monkeypatch.setattr(sync.subprocess, "Popen", _FakePopen)

    assert sync._spawn_detached_sync(str(project)) is True
    assert captured["env"].get("COGNEE_SYNC_DATASET") == "explicit-dataset"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
