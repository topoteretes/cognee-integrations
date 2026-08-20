"""Dataset precedence and persistence tests shared by Claude Code and Codex."""

from __future__ import annotations

import json


def test_scope_absent_keeps_default(suite, isolated_modules, project_dir):
    config = isolated_modules(suite, "config")
    loaded = config.load_config(str(project_dir))
    assert loaded["dataset"] == "agent_sessions"
    assert loaded["_dataset_source"] == "default"


def test_unknown_scope_keeps_default(suite, isolated_modules, project_dir, monkeypatch):
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", "repository")
    loaded = config.load_config(str(project_dir))
    assert (loaded["dataset"], loaded["_dataset_source"]) == ("agent_sessions", "default")


def test_project_scope_derives_dataset(suite, isolated_modules, project_dir, monkeypatch):
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", " Project ")
    monkeypatch.setattr(config, "derive_project_dataset", lambda workspace: "project_repo_abc123def456")
    loaded = config.load_config(str(project_dir))
    assert loaded["dataset"] == "project_repo_abc123def456"
    assert loaded["_dataset_source"] == "project"


def test_explicit_dataset_wins_over_project_scope(suite, isolated_modules, project_dir, monkeypatch):
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", "project")
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "explicit")
    loaded = config.load_config(str(project_dir))
    assert (loaded["dataset"], loaded["_dataset_source"]) == ("explicit", "env")


def test_picker_marker_wins_over_project_scope(suite, isolated_modules, project_dir, monkeypatch):
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", "project")
    selected = config._apply_project_dataset(
        {"dataset": "picked", "_dataset_source": "picker"},
        str(project_dir),
    )
    assert (selected["dataset"], selected["_dataset_source"]) == ("picked", "picker")


def test_derived_dataset_is_not_persisted_globally(suite, isolated_modules, project_dir, monkeypatch):
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", "project")
    monkeypatch.setattr(config, "derive_project_dataset", lambda workspace: "project_repo_abc123def456")
    loaded = config.load_config(str(project_dir))
    config.save_config(loaded)
    saved = json.loads(config._CONFIG_FILE.read_text(encoding="utf-8"))
    assert saved["dataset"] == "agent_sessions"
    assert "_dataset_source" not in saved
