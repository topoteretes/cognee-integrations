"""Behavioral coverage for launch-record dataset pinning."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import time

import pytest


def _publication_lock_path(common, host_key: str):
    digest = hashlib.sha256(host_key.encode("utf-8")).hexdigest()
    return common._LAUNCH_PUBLICATION_LOCK_DIR / f"{digest}.lock"


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


def test_launch_record_publication_does_not_use_improve_lock(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("launch publication reused improve_session_lock")

    monkeypatch.setattr(common, "improve_session_lock", forbidden)

    session_id, conn_uuid = common.ensure_launch_record(
        "dedicated-lock-host",
        "/repo",
        dataset="project_repo_111111111111",
        dataset_source="project",
    )

    assert common._read_map_record("dedicated-lock-host") == {
        "conn_uuid": conn_uuid,
        "created_at": common._read_map_record("dedicated-lock-host")["created_at"],
        "dataset": "project_repo_111111111111",
        "dataset_source": "project",
        "host_key": "dedicated-lock-host",
        "session_id": session_id,
        "touched": [session_id],
    }


def test_launch_publication_management_error_fails_closed_and_bounded(
    suite, isolated_modules, monkeypatch
):
    common = isolated_modules(suite, "_plugin_common")

    def denied(_path):
        raise PermissionError("lock directory denied")

    monkeypatch.setattr(common, "_open_launch_lock_file", denied, raising=False)
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="launch record publication"):
        common.ensure_launch_record(
            "denied-host",
            "/repo",
            dataset="project_repo_111111111111",
            dataset_source="project",
        )

    assert time.monotonic() - started < 0.5
    assert not common._session_map_path("denied-host").exists()


def test_stale_publication_metadata_without_a_live_lock_is_recovered(suite, isolated_modules):
    common = isolated_modules(suite, "_plugin_common")
    lock_path = _publication_lock_path(common, "stale-host")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"token": "stale-owner", "pid": 999999, "created_at": 1}),
        encoding="utf-8",
    )

    with common._launch_publication_lock("stale-host", timeout=0.2) as token:
        assert token and token != "stale-owner"
        metadata = json.loads(lock_path.read_text(encoding="utf-8"))
        assert metadata["token"] == token


def test_old_publication_owner_cannot_remove_successor_lock(suite, isolated_modules):
    common = isolated_modules(suite, "_plugin_common")
    first_acquired = threading.Event()
    release_first = threading.Event()
    second_acquired = threading.Event()
    release_second = threading.Event()
    tokens = {}

    def first_owner():
        with common._launch_publication_lock("owner-host", timeout=0.5) as token:
            tokens["first"] = token
            first_acquired.set()
            assert release_first.wait(timeout=2)

    def second_owner():
        assert first_acquired.wait(timeout=2)
        with common._launch_publication_lock("owner-host", timeout=1.0) as token:
            tokens["second"] = token
            second_acquired.set()
            assert release_second.wait(timeout=2)

    first = threading.Thread(target=first_owner)
    second = threading.Thread(target=second_owner)
    first.start()
    second.start()
    assert first_acquired.wait(timeout=2)
    release_first.set()
    assert second_acquired.wait(timeout=2)

    lock_path = _publication_lock_path(common, "owner-host")
    metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    assert metadata["token"] == tokens["second"]
    assert tokens["first"] != tokens["second"]
    assert lock_path.exists(), "the old owner's cleanup unlinked its successor"

    release_second.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert lock_path.exists(), "publication lock inode must remain stable across owners"


def test_publication_lock_recovers_after_owner_process_exits_without_cleanup(
    suite, isolated_modules
):
    common = isolated_modules(suite, "_plugin_common")
    code = f"""
import os
import sys
sys.path.insert(0, {str(suite.scripts_dir)!r})
from _plugin_common import _launch_publication_lock
with _launch_publication_lock('crashed-host', timeout=0.5) as token:
    print(token or '', flush=True)
    sys.stdin.read(1)
    os._exit(17)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        assert child.stdout is not None
        child_token = child.stdout.readline().strip()
        assert child_token
        with common._launch_publication_lock("crashed-host", timeout=0.05) as token:
            assert token is False

        assert child.stdin is not None
        child.stdin.write("x")
        child.stdin.flush()
        assert child.wait(timeout=2) == 17

        with common._launch_publication_lock("crashed-host", timeout=0.5) as token:
            assert token and token != child_token
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


def test_concurrent_new_record_publication_returns_only_the_complete_winner(
    suite, isolated_modules, monkeypatch
):
    common = isolated_modules(suite, "_plugin_common")
    publication_started = threading.Event()
    release_publication = threading.Event()
    real_dump = common.json.dump

    def delayed_record_dump(record, file_handle, *args, **kwargs):
        if isinstance(record, dict) and record.get("dataset"):
            assert not common._session_map_path("new-host").exists()
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_launch_record_new_and_replacement_modes_are_private(suite, isolated_modules):
    common = isolated_modules(suite, "_plugin_common")
    common.ensure_launch_record(
        "private-host",
        "/repo",
        dataset="project_repo_111111111111",
        dataset_source="project",
    )
    path = common._session_map_path("private-host")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    os.chmod(path, 0o644)
    updated = common._read_map_record("private-host")
    updated["touched"] = [updated["session_id"], "replacement"]
    common._write_map_record("private-host", updated)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


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
        return write_record(host_key, record)

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
