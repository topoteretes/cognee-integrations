"""Behavioral coverage for launch-record dataset pinning."""

from __future__ import annotations

import concurrent.futures
import threading
import time


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


def test_concurrent_new_record_publication_returns_only_the_complete_winner(
    suite, isolated_modules, monkeypatch
):
    common = isolated_modules(suite, "_plugin_common")
    publication_started = threading.Event()
    release_publication = threading.Event()
    real_dump = common.json.dump

    def delayed_record_dump(record, file_handle, *args, **kwargs):
        if isinstance(record, dict) and record.get("dataset"):
            publication_started.set()
            assert release_publication.wait(timeout=2), "timed out releasing first publication"
        return real_dump(record, file_handle, *args, **kwargs)

    monkeypatch.setattr(common.json, "dump", delayed_record_dump)

    candidates = (
        ("/repo-a", "project_a_111111111111"),
        ("/repo-b", "project_b_222222222222"),
    )

    def create(candidate):
        cwd, dataset = candidate
        ids = common.ensure_launch_record(
            "new-host", cwd, dataset=dataset, dataset_source="project"
        )
        return ids, common.get_launch_dataset("new-host")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(create, candidates[0])
        # The vulnerable implementation exposes the destination before JSON is
        # complete and therefore enters the delayed dump. An atomic implementation
        # may finish without entering that seam; either way, launch the contender
        # only after the first has had a chance to publish or block.
        publication_started.wait(timeout=0.2)
        second = executor.submit(create, candidates[1])
        time.sleep(0.05)
        release_publication.set()
        observed = [first.result(timeout=2), second.result(timeout=2)]

    record = common._read_map_record("new-host")
    assert record["session_id"] and record["conn_uuid"]
    assert record["dataset"] == candidates[0][1]
    assert record["dataset_source"] == "project"
    assert {ids for ids, _dataset in observed} == {(record["session_id"], record["conn_uuid"])}
    assert {dataset for _ids, dataset in observed} == {(record["dataset"], "project")}


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
    first = common.ensure_launch_record(
        "host-one", "/repo", dataset=dataset, dataset_source="project"
    )
    second = common.ensure_launch_record(
        "host-two", "/repo", dataset=dataset, dataset_source="project"
    )
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
