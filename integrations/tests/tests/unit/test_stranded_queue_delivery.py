"""A session's queued entries are delivered after it ends, and its improve follows them.

Root-caused from a real hook.log. While the server was busy each session write
took 10 to 20 seconds or timed out, and the session-end worker handled that
badly in three ways:

  * a drain pass stops at the first write that fails, and the worker made two
    passes per attempt and three attempts, so six slow writes ended delivery
    with entries still queued (42 workers gave up that way; 39 sessions were
    left with 528 queued entries between them);
  * it submitted the improve after every such attempt, entries queued or not
    (106 improves of a session whose answer had not reached the server);
  * once it gave up nothing ran for that session again: only the session
    itself, resumed, drained its queue, and no line said how much was left.

Contract pinned here (Claude Code plugin):
  * at a session's end a drain pass that delivered something is followed by the
    next one, so a slow server is waited out and the improve is submitted once,
    after the last queued entry;
  * two passes in a row that deliver nothing end the attempt, as before;
  * a wait for another drain's lock is not a failed pass;
  * SessionStart starts one detached worker that delivers the queues of ended
    sessions of the same server, oldest first, one session at a time, and
    submits no improve for a queue it could not move;
  * a worker that gives up logs ``delivery_gave_up`` with the count left;
  * a healthy server sees the same requests as before.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error

import pytest

SESSION_KEY = "5c492456-a515-4807-8cad-9a0d4c79a2f7"
SESSION_ID = f"claude_{SESSION_KEY}"
DATASET = "agent_sessions"
SERVER = "http://127.0.0.1:8011"


def _trace(n: int) -> dict:
    return {
        "type": "trace",
        "origin_function": "Read",
        "status": "success",
        "method_params": {"file_path": f"inventory/file{n}.py"},
        "method_return_value": f"contents {n}",
    }


QA = {"type": "qa", "question": "why is the total wrong?", "answer": "fixed", "context": "{}"}


class FakeServer:
    """Stands in for the HTTP seams; every write costs ``write_seconds`` of fake time."""

    def __init__(self, clock: list, write_seconds: float = 0.0) -> None:
        self.clock = clock
        self.write_seconds = write_seconds
        self.requests: list[str] = []
        self.failures: list = []  # one per write, consumed in order; None = success
        self.down = False

    def remember_entry(self, dataset, session_id, entry, **kwargs):
        self.clock[0] += self.write_seconds
        failure = self.failures.pop(0) if self.failures else None
        if self.down:
            failure = urllib.error.URLError("Connection refused")
        if failure is not None:
            raise failure
        self.requests.append("qa" if entry.get("type") == "qa" else "trace")

    def improve(self, dataset, session_id, **kwargs):
        self.requests.append("improve")
        return {"ok": True}


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: queue delivery after the session is Claude Code only")
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_SESSION_KEY", SESSION_KEY)
    common._events = []
    monkeypatch.setattr(
        common, "hook_log", lambda ev, detail=None: common._events.append((ev, detail or {}))
    )
    return common


@pytest.fixture
def server(pc, monkeypatch):
    """A fake server and a fake clock: sleeping and writing both move the clock."""
    clock = [1000.0]
    fake = FakeServer(clock)
    fake.sleeps = []

    def _sleep(seconds):
        fake.sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(pc.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pc.time, "sleep", _sleep)
    monkeypatch.setattr(pc, "_local_api_url", lambda: SERVER)
    monkeypatch.setattr(pc, "_backend_reachable", lambda url, **kw: not fake.down)
    monkeypatch.setattr(pc, "remember_entry_via_http", fake.remember_entry)
    monkeypatch.setattr(pc, "improve_session_via_http", fake.improve)
    monkeypatch.setattr(pc, "ensure_dataset_via_http", lambda dataset: None)
    monkeypatch.setattr(pc, "refresh_credits", lambda *a, **k: {})
    return fake


def _queue(pc, entries) -> None:
    for entry in entries:
        pc.append_warmup_entry(DATASET, SESSION_ID, entry)


def _queued(pc) -> int:
    state = pc._load_json_file(pc._bridge_file(SESSION_ID)).get(f"{DATASET}:{SESSION_ID}") or {}
    return len(state.get("pending_entries") or [])


def _events(pc, name: str) -> list[dict]:
    return [detail for event, detail in pc._events if event == name]


# ── the session's own end ─────────────────────────────────────────────────────


def test_the_incident_slow_writes_are_waited_out_and_the_improve_follows_the_answer(pc, server):
    """Four traces and the answer are still queued when the session ends; every
    write takes 27 s, some time out. Before: two passes, an improve without the
    answer, and after three such attempts the queue was abandoned."""
    _queue(pc, [_trace(n) for n in range(4)] + [QA])
    server.write_seconds = 27.0
    timeout = TimeoutError("timed out")
    server.failures = [None, timeout, None, None, timeout, None, timeout, None]

    outcome = pc.run_session_improve_detailed(DATASET, SESSION_ID, trigger="final")

    assert outcome == {"ok": True, "reason": "", "error": ""}
    assert server.requests == ["trace", "trace", "trace", "trace", "qa", "improve"]
    assert _queued(pc) == 0
    assert [fired["reason"] for fired in _events(pc, "improve_fired")] == [""]
    # The pause between passes is the existing one, nothing new.
    assert set(server.sleeps) == {pc._DRAIN_RETRY_PAUSE_SECONDS}


def test_no_improve_while_a_slow_drain_is_still_delivering(pc, server):
    """A pass that ran out of its budget delivered something: the next pass
    follows, and the improve waits for the last entry."""
    _queue(pc, [_trace(n) for n in range(7)] + [QA])
    server.write_seconds = 50.0  # the 120 s budget fits three writes per pass

    pc.run_session_improve_detailed(DATASET, SESSION_ID, trigger="final")

    assert server.requests == ["trace"] * 7 + ["qa", "improve"]
    assert len(_events(pc, "warmup_drain_budget_exceeded")) == 2
    assert not _events(pc, "improve_incomplete_drain")


def test_two_passes_that_deliver_nothing_end_the_attempt(pc, server, monkeypatch):
    _queue(pc, [_trace(0), QA])
    server.down = True  # the writes are refused...
    monkeypatch.setattr(pc, "_backend_reachable", lambda url, **kw: True)  # ...the probe is not

    outcome = pc.run_session_improve_detailed(DATASET, SESSION_ID, trigger="final")

    assert outcome["ok"] is False and outcome["reason"] == "incomplete_drain"
    assert len(_events(pc, "warmup_drain_error")) == 2, "one retry, as before"
    assert _queued(pc) == 2
    assert "Connection refused" in pc.drain_stop_reason()


def test_a_wait_for_another_drains_lock_is_not_a_failed_pass(pc, server, monkeypatch):
    """The lock is machine-wide. A session end that met another session's drain
    used to spend both passes on the lock and submit the improve with every
    entry still queued."""
    _queue(pc, [_trace(0), QA])
    real = pc._try_acquire_drain_lock
    held = [True, True, True]  # three passes find the lock taken
    monkeypatch.setattr(
        pc, "_try_acquire_drain_lock", lambda: False if held and held.pop() else real()
    )

    outcome = pc.run_session_improve_detailed(DATASET, SESSION_ID, trigger="final")

    assert outcome["ok"] is True
    assert server.requests == ["trace", "qa", "improve"]
    assert len(_events(pc, "warmup_drain_skipped_locked")) == 3


def test_a_long_drain_keeps_its_lock_fresh(pc, server, monkeypatch):
    """A session-end drain runs for minutes; its lock must not look abandoned
    after ``_DRAIN_LOCK_STALE_SECONDS`` or a second drain would run beside it."""
    _queue(pc, [_trace(0), _trace(1)])
    stamps = []
    real = server.remember_entry

    def _write(*args, **kwargs):
        stamps.append(pc._DRAIN_LOCK.stat().st_mtime)
        old = time.time() - 2 * pc._DRAIN_LOCK_STALE_SECONDS
        import os

        os.utime(pc._DRAIN_LOCK, (old, old))  # as if this write took two minutes
        return real(*args, **kwargs)

    monkeypatch.setattr(pc, "remember_entry_via_http", _write)
    pc.drain_warmup_entries(DATASET, SESSION_ID)

    assert time.time() - stamps[1] < pc._DRAIN_LOCK_STALE_SECONDS


def test_other_triggers_keep_the_single_retry(pc, server):
    """Only a session that is over waits a slow server out: a mid-session
    trigger has a next one, and a manual sync has the user waiting."""
    _queue(pc, [_trace(n) for n in range(7)] + [QA])
    server.write_seconds = 50.0

    outcome = pc.run_session_improve_detailed(DATASET, SESSION_ID, trigger="manual")

    assert outcome["reason"] == "incomplete_drain"
    assert server.requests == ["trace"] * 6 + ["improve"]  # two passes of three writes
    assert _queued(pc) == 2


def test_nothing_changes_for_a_healthy_server(pc, server):
    _queue(pc, [_trace(0), _trace(1), QA])

    outcome = pc.run_session_improve_detailed(DATASET, SESSION_ID, trigger="final")

    assert outcome == {"ok": True, "reason": "", "error": ""}
    assert server.requests == ["trace", "trace", "qa", "improve"]
    assert server.sleeps == []


def test_nothing_changes_for_a_healthy_server_with_an_empty_queue(pc, server):
    pc.run_session_improve_detailed(DATASET, SESSION_ID, trigger="final")

    assert server.requests == ["improve"]
    assert server.sleeps == []


# ── an ended session's queue, picked up later ─────────────────────────────────


def test_a_replay_that_moves_nothing_submits_no_improve_and_the_next_one_does(pc, server):
    """The session's own end already improved what the server holds. A replay
    that delivers nothing has nothing new to distil; one that delivers the
    queue submits the improve it was owed, once, after the answer."""
    _queue(pc, [_trace(0), QA])
    server.failures = [urllib.error.HTTPError(SERVER, 503, "busy", {}, None)]

    stalled = pc.run_session_improve_detailed(DATASET, SESSION_ID, trigger="replay")

    assert stalled == {"ok": False, "reason": "incomplete_drain", "error": ""}
    assert server.requests == []
    assert _events(pc, "improve_incomplete_drain")[-1]["submitted"] is False
    assert not _events(pc, "improve_fired")
    assert pc.drain_stop_reason() == "backoff"  # the retry pass met the 503 backoff
    assert _queued(pc) == 2

    # Past the backoff the server answers again.
    path = pc._bridge_file(SESSION_ID)
    cache = json.loads(path.read_text(encoding="utf-8"))
    cache[f"{DATASET}:{SESSION_ID}"]["drain_fail_at"] -= 3600
    path.write_text(json.dumps(cache), encoding="utf-8")

    delivered = pc.run_session_improve_detailed(DATASET, SESSION_ID, trigger="replay")

    assert delivered["ok"] is True
    assert server.requests == ["trace", "qa", "improve"]
    assert pc.read_improve_state(SESSION_ID)["trigger"] == "replay"


def test_a_replay_that_delivered_part_of_the_queue_improves_that_part(pc, server):
    _queue(pc, [_trace(0), _trace(1), QA])
    refused = urllib.error.HTTPError(SERVER, 401, "Unauthorized", {}, None)
    server.failures = [None, refused]

    outcome = pc.run_session_improve_detailed(DATASET, SESSION_ID, trigger="replay")

    assert outcome["ok"] is False and outcome["reason"] == "incomplete_drain"
    assert server.requests == ["trace", "improve"]
    assert _queued(pc) == 2


# ── the worker SessionStart starts ────────────────────────────────────────────


@pytest.fixture
def sync(suite, hook_module, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: queue delivery after the session is Claude Code only")
    module = hook_module(suite, "sync-session-to-graph.py")
    monkeypatch.setenv("COGNEE_SESSION_KEY", "the-new-session")
    monkeypatch.setenv("COGNEE_BASE_URL", SERVER)
    module._events = []
    monkeypatch.setattr(
        module, "hook_log", lambda ev, detail=None: module._events.append((ev, detail or {}))
    )
    return module


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _ended_session(key: str, entries: int, *, queued_at: float, server: str = SERVER, pid=None):
    """A launch record and a replay buffer as an ended session leaves them."""
    import os

    import _plugin_common as common

    session_id = f"claude_{key}"
    record = {"session_id": session_id, "dataset": DATASET, "host_key": key}
    record["host_pid"] = _dead_pid() if pid is None else pid
    if server:
        record["base_url"] = server
    common._write_map_record(key, record)
    queue = [{**_trace(n), common._BUFFERED_AT_KEY: queued_at + n} for n in range(entries)]
    common._write_json_file(
        common._BRIDGE_DIR / f"{key}.json",
        {f"{DATASET}:{session_id}": {"pending_entries": queue}},
    )
    assert os.path.exists(common._BRIDGE_DIR / f"{key}.json")


def _children(sync, monkeypatch, *, reachable=lambda: True) -> list[dict]:
    import _plugin_common as common

    runs: list[dict] = []

    def _run(argv, **kwargs):
        runs.append({"argv": argv, "env": kwargs["env"], "running": len(running)})
        return subprocess.CompletedProcess(argv, 0)

    running: list = []
    monkeypatch.setattr(sync.subprocess, "run", _run)
    monkeypatch.setattr(common, "_backend_reachable", lambda url, **kw: reachable())
    return runs


def test_ended_sessions_queues_are_delivered_oldest_first_one_at_a_time(sync, monkeypatch):
    now = time.time()
    _ended_session("newer", 2, queued_at=now - 600)
    _ended_session("older", 5, queued_at=now - 13 * 86400)
    runs = _children(sync, monkeypatch)
    monkeypatch.setenv("COGNEE_SYNC_SESSION_ID", "claude_the-new-session")
    monkeypatch.setenv("COGNEE_UNREGISTER_ON_FINISH", "1")

    sync._deliver_stranded()

    # subprocess.run waits for each child: never two writers.
    assert [run["env"]["COGNEE_SESSION_KEY"] for run in runs] == ["older", "newer"]
    for run in runs:
        assert run["argv"][-1] == "--detached-final"
        assert run["env"]["COGNEE_SYNC_STRANDED"] == "1"
        assert run["env"]["COGNEE_SYNC_START_DELAY"] == "0"
        # The new session's own target and teardown must not leak into the child.
        assert "COGNEE_SYNC_SESSION_ID" not in run["env"]
        assert "COGNEE_UNREGISTER_ON_FINISH" not in run["env"]
    assert sync._events == [("stranded_queues", {"sessions": 2, "entries": 7, "skipped": {}})]
    assert not sync._STRANDED_PIDFILE.exists()


def test_only_ended_sessions_of_this_server_are_delivered(sync, monkeypatch):
    import os

    now = time.time()
    _ended_session("ended", 1, queued_at=now)
    _ended_session("still-running", 2, queued_at=now, pid=os.getpid())
    _ended_session("the-new-session", 3, queued_at=now)
    _ended_session("cloud-tenant", 4, queued_at=now, server="https://tenant.example.com")
    _ended_session("before-the-url-was-recorded", 5, queued_at=now, server="")
    import _plugin_common as common

    common._write_json_file(
        common._BRIDGE_DIR / "no-record.json",
        {"ds:claude_x": {"pending_entries": [_trace(0)]}},
    )
    runs = _children(sync, monkeypatch)

    sync._deliver_stranded()

    assert [run["env"]["COGNEE_SESSION_KEY"] for run in runs] == ["ended"]
    assert sync._events[0][1]["skipped"] == {
        "session_live": 5,
        "other_server": 9,
        "no_launch_record": 1,
    }
    # Nothing was dropped: every entry is still counted as awaiting replay.
    assert common.warmup_backlog()["pending"] == 16


def test_an_unreachable_server_stops_the_worker_with_one_line(sync, monkeypatch):
    now = time.time()
    _ended_session("a", 2, queued_at=now - 30)
    _ended_session("b", 3, queued_at=now - 20)
    _ended_session("c", 4, queued_at=now - 10)
    answers = iter([True, False])
    runs = _children(sync, monkeypatch, reachable=lambda: next(answers))

    sync._deliver_stranded()

    assert [run["env"]["COGNEE_SESSION_KEY"] for run in runs] == ["a"]
    assert sync._events[-1] == (
        "stranded_delivery_stopped",
        {"sessions": 2, "left": 7, "reason": "unreachable"},
    )
    import _plugin_common as common

    assert common.warmup_backlog()["pending"] == 9  # the queues are intact


def test_a_second_worker_leaves_the_first_one_to_it(sync, monkeypatch):
    import os

    _ended_session("ended", 1, queued_at=time.time())
    runs = _children(sync, monkeypatch)
    sync._STRANDED_PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    sync._STRANDED_PIDFILE.write_text(str(os.getpid()), encoding="utf-8")

    sync._deliver_stranded()
    assert runs == [] and sync._STRANDED_PIDFILE.exists()

    sync._STRANDED_PIDFILE.write_text(str(_dead_pid()), encoding="utf-8")  # it crashed
    sync._deliver_stranded()
    assert len(runs) == 1


def test_no_stranded_queue_means_no_request_no_child_and_no_log_line(sync, monkeypatch):
    import os

    import _plugin_common as common

    _ended_session("ended-and-delivered", 0, queued_at=time.time())
    probes = []
    runs = _children(sync, monkeypatch, reachable=lambda: probes.append(1) or True)
    monkeypatch.setattr(
        common.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("a healthy session start must send nothing"),
    )

    sync._deliver_stranded()

    assert runs == [] and probes == [] and sync._events == []
    assert not os.path.exists(sync._STRANDED_PIDFILE)


def test_capture_turned_off_delivers_nothing(sync, monkeypatch):
    """With capture off the drain sends nothing, so the queue would never empty
    and every session start would submit an improve for it."""
    _ended_session("ended", 2, queued_at=time.time())
    runs = _children(sync, monkeypatch)
    monkeypatch.setenv("COGNEE_CAPTURE", "off")
    import _capture_policy

    if _capture_policy.capture_enabled():
        pytest.skip("this build has no capture switch")

    sync._deliver_stranded()

    assert runs == []


# ── giving up is one visible line ─────────────────────────────────────────────


def _run_final(sync, monkeypatch, outcome: dict, *, stop_reason: str = "") -> None:
    import _plugin_common as common

    monkeypatch.setattr(
        sync, "_load_resolved", lambda: (SESSION_ID, DATASET, "", "", False, True, SESSION_KEY)
    )
    monkeypatch.setattr(sync, "http_api_ready", lambda: True)
    monkeypatch.setattr(sync, "phase_begin", lambda *a, **k: None)
    monkeypatch.setattr(sync, "phase_end", lambda *a, **k: None)
    monkeypatch.setattr(sync, "run_session_improve_detailed", lambda d, s, **kw: outcome)
    monkeypatch.setattr(sync, "drain_stop_reason", lambda: stop_reason)
    monkeypatch.setattr(sync.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sync, "_CLAIMED_MARKER", None)
    monkeypatch.setattr(sync, "_LEFT_REASON", "")
    monkeypatch.setattr(sys, "argv", ["sync-session-to-graph.py", "--detached-final"])
    monkeypatch.setenv("COGNEE_SESSION_KEY", SESSION_KEY)
    monkeypatch.setenv("COGNEE_SYNC_START_DELAY", "0")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    sync.main()


def _gave_up(sync) -> list[dict]:
    return [detail for event, detail in sync._events if event == "delivery_gave_up"]


def test_an_unreachable_server_leaves_the_queue_and_logs_the_give_up(sync, monkeypatch):
    import _plugin_common as common

    monkeypatch.setenv("COGNEE_SESSION_KEY", SESSION_KEY)
    for entry in [_trace(0), _trace(1), QA]:
        common.append_warmup_entry(DATASET, SESSION_ID, entry)

    _run_final(
        sync, monkeypatch, {"ok": False, "reason": "unreachable", "error": "backend unreachable"}
    )

    assert _gave_up(sync) == [{"session": SESSION_KEY, "left": 3, "reason": "unreachable"}]
    assert [event for event, _ in sync._events].count("sync_failed") == 3
    # Released, so the next session start's worker may take the session up.
    assert any(event == "final_sync_once_released" for event, _ in sync._events)
    assert common.warmup_backlog()["pending"] == 3


@pytest.mark.parametrize("stop_reason", ["http_401", "locked", "timed out"])
def test_the_give_up_names_why_the_drain_stopped(sync, monkeypatch, stop_reason):
    import _plugin_common as common

    monkeypatch.setenv("COGNEE_SESSION_KEY", SESSION_KEY)
    common.append_warmup_entry(DATASET, SESSION_ID, QA)

    _run_final(
        sync,
        monkeypatch,
        {"ok": False, "reason": "incomplete_drain", "error": ""},
        stop_reason=stop_reason,
    )

    assert _gave_up(sync) == [{"session": SESSION_KEY, "left": 1, "reason": stop_reason}]


def test_a_failed_improve_with_an_empty_queue_is_not_a_delivery_give_up(sync, monkeypatch):
    _run_final(sync, monkeypatch, {"ok": False, "reason": "failed", "error": "timed out"})

    assert _gave_up(sync) == []
    assert [event for event, _ in sync._events].count("sync_failed") == 3


def test_the_worker_runs_the_final_sync_as_a_replay(sync, monkeypatch):
    seen = []
    monkeypatch.setenv("COGNEE_SYNC_STRANDED", "1")
    _run_final(sync, monkeypatch, {"ok": True, "reason": "", "error": ""})
    monkeypatch.setattr(
        sync,
        "run_session_improve_detailed",
        lambda d, s, **kw: seen.append(kw["trigger"]) or {"ok": True, "reason": "", "error": ""},
    )
    monkeypatch.setattr(sync, "_claim_final_sync_once", lambda: True)
    sync.main()
    monkeypatch.delenv("COGNEE_SYNC_STRANDED")
    sync.main()

    assert seen == ["replay", "final"]


# ── SessionStart ──────────────────────────────────────────────────────────────


def test_session_start_starts_the_worker_detached(suite, hook_module, monkeypatch):
    """Detached from the hook: it outlives SessionStart, and Claude Code itself."""
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: queue delivery after the session is Claude Code only")
    start = hook_module(suite, "session-start.py")
    spawned = []
    monkeypatch.setattr(
        start.subprocess, "Popen", lambda argv, **kwargs: spawned.append((argv, kwargs))
    )

    start._spawn_stranded_delivery(session_key="new", api_key="secret-key", service_url=SERVER)

    ((argv, kwargs),) = spawned
    assert argv[1].endswith("sync-session-to-graph.py") and argv[2] == "--deliver-stranded"
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert "secret-key" not in " ".join(argv), "credentials stay out of the process list"
    assert kwargs["env"]["COGNEE_API_KEY"] == "secret-key"
    assert kwargs["env"]["COGNEE_BASE_URL"] == SERVER
    assert kwargs["env"]["COGNEE_SESSION_KEY"] == "new"
