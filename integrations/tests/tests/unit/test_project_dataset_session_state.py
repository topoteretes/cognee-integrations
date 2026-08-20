"""Behavioral coverage for launch-record dataset pinning."""

from __future__ import annotations

import concurrent.futures
import threading


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


def test_new_dataset_record_persists_session_and_connection_ids(suite, isolated_modules):
    common = isolated_modules(suite, "_plugin_common")
    session_id, conn_uuid = common.ensure_launch_record(
        "host-one",
        "/repo",
        dataset="project_repo_111111111111",
        dataset_source="project",
    )
    record = common._read_map_record("host-one")
    assert record["session_id"] == session_id
    assert record["conn_uuid"] == conn_uuid
    assert record["session_id"]
    assert record["conn_uuid"]


def test_concurrent_legacy_pins_keep_the_first_dataset(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    common._write_map_record(
        "host-one",
        {
            "session_id": "legacy-session",
            "conn_uuid": "conn_legacy",
            "host_key": "host-one",
        },
    )
    concurrent_writes = threading.Barrier(2)
    write_order = []
    write_order_lock = threading.Lock()
    write_record = common._write_map_record

    def synchronized_write(host_key, record):
        if record.get("dataset"):
            try:
                concurrent_writes.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass
            with write_order_lock:
                write_order.append(record["dataset"])
        write_record(host_key, record)

    monkeypatch.setattr(common, "_write_map_record", synchronized_write)

    def pin(dataset):
        common.ensure_launch_record("host-one", "/repo", dataset=dataset, dataset_source="project")
        return common.get_launch_dataset("host-one")[0]

    datasets = ("project_a_111111111111", "project_b_222222222222")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        observed = list(executor.map(pin, datasets))

    assert common.get_launch_dataset("host-one")[0] == write_order[0]
    assert len(set(observed)) == 1
    assert common.get_launch_dataset("host-one")[0] == observed[0]


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
