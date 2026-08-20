"""Behavioral coverage for launch-record dataset pinning."""

from __future__ import annotations


def test_launch_record_pins_dataset_and_source(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(
        common,
        "_json_http_request",
        lambda *a, **k: (_ for _ in ()).throw(OSError("offline test")),
    )
    common.ensure_launch_record(
        "host-one",
        "/repo",
        dataset="project_repo_111111111111",
        dataset_source="project",
    )
    resolved = common.load_resolved("host-one")
    assert resolved["dataset"] == "project_repo_111111111111"
    assert resolved["dataset_source"] == "project"


def test_later_resolution_cannot_switch_pinned_dataset(suite, isolated_modules):
    common = isolated_modules(suite, "_plugin_common")
    common.ensure_launch_record(
        "host-one", "/repo-a", dataset="project_a_111111111111", dataset_source="project"
    )
    common.ensure_launch_record(
        "host-one", "/repo-b", dataset="project_b_222222222222", dataset_source="project"
    )
    assert common.get_launch_dataset("host-one") == ("project_a_111111111111", "project")


def test_conversations_keep_distinct_sessions_in_one_dataset(suite, isolated_modules):
    common = isolated_modules(suite, "_plugin_common")
    dataset = "project_repo_111111111111"
    first = common.ensure_launch_record("host-one", "/repo", dataset=dataset, dataset_source="project")
    second = common.ensure_launch_record("host-two", "/repo", dataset=dataset, dataset_source="project")
    assert first[0] != second[0]
    assert common.get_launch_dataset("host-one")[0] == common.get_launch_dataset("host-two")[0]


def test_exit_worker_receives_pinned_dataset(suite, hook_module, monkeypatch):
    watcher = hook_module(suite, "exit-watcher.py")
    captured = {}

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(watcher.subprocess, "Popen", FakeProcess)
    watcher._spawn_sync(
        "session-one",
        "project_repo_111111111111",
        session_key="host-one",
    )
    assert captured["env"]["COGNEE_SYNC_DATASET"] == "project_repo_111111111111"
