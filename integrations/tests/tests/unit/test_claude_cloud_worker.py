"""Durable cloud work: outage, recovery, retention, and truthful improve outcomes."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def worker(suite, isolated_modules, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("Cloud worker")
    worker = isolated_modules(suite, "_cloud_worker")
    monkeypatch.setenv("COGNEE_SESSION_KEY", "host-a")
    monkeypatch.setattr(
        worker, "target", lambda: {"base_url": "https://cloud.example", "principal": "key"}
    )
    monkeypatch.setattr(worker, "ensure_worker", lambda: None)
    monkeypatch.setattr(worker, "log", lambda *a, **kw: None)
    return worker


def test_capture_outage_retains_then_recovers_in_order(worker):
    paths = [
        worker.enqueue("ds", "sid", {"type": "qa", "answer": str(i)}, "answer") for i in range(3)
    ]
    created = worker.read(paths[0])["created_at"]

    def offline(item):
        raise OSError("offline")

    assert worker.drain(offline, now=created) == 0
    assert all(path.exists() for path in paths)
    assert worker.read(paths[0])["attempts"] == 1
    delivered = []

    def send(item):
        delivered.append(item["entry"]["answer"])
        return {"entry_id": "saved"}

    assert worker.drain(send, now=created + 6) == 3
    assert delivered == ["0", "1", "2"]
    assert not any(path.exists() for path in paths)


def test_expiry_archives_and_logs_without_silent_deletion(worker, monkeypatch):
    events = []
    monkeypatch.setattr(worker, "log", lambda event, **kw: events.append(event))
    path = worker.enqueue("ds", "sid", {"type": "qa", "answer": "retain me"}, "answer")
    worker.drain(
        lambda _: pytest.fail("expired event was sent"),
        now=worker.read(path)["created_at"] + 7 * 86400 + 1,
    )
    archived = path.parent / "expired" / path.name
    assert worker.read(archived)["entry"]["answer"] == "retain me"
    assert "cloud_work_expired" in events


def test_ambiguous_success_is_verified_before_retry(worker):
    path = worker.enqueue("ds", "sid", {"type": "qa", "answer": "once"}, "answer")
    item = worker.read(path)
    item["ambiguous"] = True
    worker.atomic_json(path, item)
    assert worker.drain(lambda _: pytest.fail("duplicate POST"), verify=lambda _: True) == 1


def test_idempotent_retry_keeps_event_and_request_after_lost_ack(worker):
    paths = [
        worker.enqueue("ds", "sid", {"type": "qa", "answer": "same"}, "answer") for _ in range(2)
    ]
    ids = [worker.read(path)["client_event_id"] for path in paths]
    assert len(set(ids)) == 2
    requests = []
    receipts = {}

    def prepare(item):
        return {
            "idempotent": True,
            "request": {"entry": item["entry"], "client_event_id": item["client_event_id"]},
        }

    def send(item):
        requests.append(item["request"])
        event = item["client_event_id"]
        if event not in receipts:
            receipts[event] = {"entry_id": event, "client_event_id": event, "payload_hash": "hash"}
            raise TimeoutError("server committed before ACK")
        return receipts[event]

    def never_verify(_):
        pytest.fail("content matching must not suppress distinct events")

    assert worker.drain(send, prepare=prepare, verify=never_verify) == 0
    first = worker.read(paths[0])
    assert first["ambiguous"] and first["idempotent"]
    assert (
        worker.drain(send, now=first["next_attempt"] + 1, verify=never_verify, prepare=prepare) == 1
    )
    second = worker.read(paths[1])
    assert (
        worker.drain(send, now=second["next_attempt"] + 1, verify=never_verify, prepare=prepare)
        == 1
    )
    assert requests[0] == requests[1] and requests[2] == requests[3]
    assert len(receipts) == 2 and not any(p.exists() for p in paths)


def test_idempotent_capture_requires_matching_receipt(worker):
    path = worker.enqueue("ds", "sid", {"type": "qa", "answer": "saved?"}, "answer")
    item = worker.read(path)
    item["idempotent"] = True
    worker.atomic_json(path, item)
    assert worker.drain(lambda _: {"entry_id": "old-server-response"}) == 0
    assert path.exists()


def test_changed_payload_conflict_is_quarantined(worker):
    import io
    import urllib.error

    path = worker.enqueue("ds", "sid", {"type": "qa", "answer": "changed"}, "answer")
    item = worker.read(path)
    item["idempotent"] = True
    worker.atomic_json(path, item)

    def conflict(_):
        raise urllib.error.HTTPError(
            "https://example",
            409,
            "conflict",
            {},
            io.BytesIO(b'{"code":"capture_payload_conflict"}'),
        )

    assert worker.drain(conflict) == 0
    assert not path.exists() and (path.parent / "expired" / path.name).exists()


def test_wrong_tenant_or_principal_never_receives_spool(worker, monkeypatch):
    path = worker.enqueue("ds", "sid", {"type": "qa", "answer": "private"}, "answer")
    monkeypatch.setattr(
        worker, "target", lambda: {"base_url": "https://other.example", "principal": "other"}
    )
    assert worker.drain(lambda _: pytest.fail("wrong tenant")) == 0
    assert path.exists()


@pytest.mark.parametrize(
    "result,ok,reason",
    [
        ({}, False, "busy"),
        ({"ok": True}, False, "unsupported_result"),
        ({"stages": [{"status": "completed"}], "finished": False}, False, "running"),
        ({"stages": [{"status": "errored"}]}, False, "errored"),
        ({"stages": [{"status": "skipped", "reason": "lock_held"}]}, False, "busy"),
        ({"stages": [{"status": "skipped", "reason": "no_session_ids"}]}, False, "skipped"),
        (
            {
                "stages": [
                    {"status": "completed"},
                    {"status": "skipped", "reason": "disabled_by_config"},
                ]
            },
            True,
            "completed",
        ),
    ],
)
def test_improve_result_contract(worker, result, ok, reason):
    assert worker.improve_outcome(result)["ok"] is ok
    assert worker.improve_outcome(result)["reason"] == reason


def test_improve_waits_for_capture_and_retries_unconfirmed(worker):
    capture = worker.enqueue("ds", "sid", {"type": "qa", "answer": "fact"}, "answer")
    worker.request_improve("ds", "sid", "final")
    assert worker.process_improves(lambda _: pytest.fail("capture is still pending")) == 0
    capture.unlink()
    assert worker.process_improves(lambda _: {"ok": True}) == 0
    marker = next((worker.root() / "improve-wanted").glob("*.json"))
    due = worker.read(marker)["next_attempt"]
    assert (
        worker.process_improves(lambda _: {"stages": [{"status": "completed"}]}, now=due + 1) == 1
    )
    assert not marker.exists()
    data = json.loads((worker.session_dir("host-a") / "stats.json").read_text())
    assert data["improves_landed"] == 1


def test_concurrent_request_survives_inflight_completion(worker):
    worker.request_improve("ds", "sid", "final")

    def improve(item):
        worker.request_improve("ds", "sid", "final")
        return {"stages": [{"status": "completed"}]}

    worker.process_improves(improve)
    assert list((worker.root() / "improve-wanted").glob("*.json"))


def test_idle_does_not_downgrade_final_request(worker):
    worker.request_improve("ds", "sid", "final")
    worker.request_improve("ds", "sid", "idle")
    marker = next((worker.root() / "improve-wanted").glob("*.json"))
    assert worker.read(marker)["reason"] == "final"


@pytest.mark.parametrize("reason", ["final", "precompact", "manual", "switch"])
def test_explicit_improve_resets_an_old_backoff(worker, reason):
    path = worker.request_improve("ds", "sid", "idle")
    item = worker.read(path)
    item.update(attempts=10, next_attempt=worker.time.time() + 3600)
    worker.atomic_json(path, item)
    assert worker.request_improve("ds", "sid", reason) == path
    refreshed = worker.read(path)
    assert refreshed["attempts"] == refreshed["next_attempt"] == 0
    assert refreshed["reason"] == reason
    assert worker.process_improves(lambda _: {"stages": [{"status": "completed"}]}) == 1


def test_busy_retries_stay_short_and_expose_status(worker):
    path = worker.request_improve("ds", "sid", "manual")
    now = worker.time.time()
    for _ in range(12):
        assert worker.process_improves(lambda _: {}, now=now) == 0
        item = worker.read(path)
        assert item["next_attempt"] == now + 5 and item["attempts"] == 0
        assert worker.improve_wait_detail(path, now=now) == "Last result: busy; next retry in 5s."
        now += 5


def test_worker_rotates_and_reopens_both_inherited_log_handles(worker, monkeypatch):
    path = worker.root() / "cloud-worker.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old log beyond the cap")
    monkeypatch.setenv("COGNEE_PLUGIN_LOG_MAX_BYTES", "10")
    handles = []
    monkeypatch.setattr(worker.os, "dup2", lambda source, target: handles.append(target))
    worker._refresh_console_log()
    assert handles == [1, 2]
    assert path.read_text() == "" and path.with_name(path.name + ".1").exists()


class _HTTPFailure(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


def test_permanently_rejected_capture_never_blocks_the_queue(worker):
    bad = worker.enqueue("ds", "sid-a", {"type": "qa", "answer": "too big"}, "answer")
    good = worker.enqueue("ds", "sid-b", {"type": "qa", "answer": "fine"}, "answer")
    sent = []

    def send(item):
        if item["entry"]["answer"] == "too big":
            raise _HTTPFailure(413)
        sent.append(item["entry"]["answer"])
        return {"entry_id": "saved"}

    assert worker.drain(send) == 1
    assert sent == ["fine"]
    assert not bad.exists() and (bad.parent / "expired" / bad.name).exists()
    assert not good.exists()


def test_a_session_waiting_on_retry_holds_only_its_own_events(worker):
    first = worker.enqueue("ds", "sid-a", {"type": "qa", "answer": "a1"}, "answer")
    later = worker.enqueue("ds", "sid-a", {"type": "qa", "answer": "a2"}, "answer")
    other = worker.enqueue("ds", "sid-b", {"type": "qa", "answer": "b1"}, "answer")
    sent = []

    def send(item):
        if item["entry"]["answer"] == "a1":
            raise _HTTPFailure(404)
        sent.append(item["entry"]["answer"])
        return {"entry_id": "saved"}

    now = worker.read(first)["created_at"]
    assert worker.drain(send, now=now) == 1
    assert sent == ["b1"]  # a2 stays behind a1, b1 is not held up
    assert first.exists() and later.exists() and not other.exists()
    # While a1 waits for its retry, the rest of sid-a waits with it.
    assert worker.drain(lambda _: pytest.fail("sent out of order"), now=now + 1) == 0
    assert worker.drain(lambda _: {"entry_id": "saved"}, now=now + 3600) == 2


def test_server_wide_failure_ends_the_pass(worker):
    worker.enqueue("ds", "sid-a", {"type": "qa", "answer": "a"}, "answer")
    worker.enqueue("ds", "sid-b", {"type": "qa", "answer": "b"}, "answer")
    calls = []

    def send(item):
        calls.append(item["session_id"])
        raise _HTTPFailure(503)

    assert worker.drain(send) == 0
    assert calls == ["sid-a"]


def test_queue_still_ages_out_while_capture_is_off(worker, monkeypatch):
    old = worker.enqueue("ds", "sid", {"type": "qa", "answer": "old"}, "answer")
    fresh = worker.enqueue("ds", "sid", {"type": "qa", "answer": "fresh"}, "answer")
    item = worker.read(old)
    item["created_at"] -= 7 * 86400 + 1
    worker.atomic_json(old, item)
    monkeypatch.setenv("COGNEE_CAPTURE", "false")
    assert worker.drain(lambda _: pytest.fail("capture is off")) == 0
    assert not old.exists() and (old.parent / "expired" / old.name).exists()
    assert fresh.exists()


def test_worker_idles_out_only_without_work_or_recent_sessions(worker):
    assert not worker.has_pending_work()
    path = worker.enqueue("ds", "sid", {"type": "qa", "answer": "x"}, "answer")
    assert worker.has_pending_work()
    path.unlink()
    launch = worker.session_dir("host-a") / "worker.json"
    worker.atomic_json(launch, {"activity_at": 1_000})
    assert worker.has_pending_work(now=1_000 + 60)
    assert not worker.has_pending_work(now=1_000 + worker.IDLE_EXIT_SECONDS + 1)


COMPLETED = {"stages": [{"status": "completed"}]}


def test_automatic_improves_share_the_local_cooldown_and_new_entry_gate(worker, monkeypatch):
    import _plugin_common as common

    calls = []

    def improve(item):
        calls.append(item["reason"])
        return COMPLETED

    common.bump_turn_counter("sid")
    worker.request_improve("ds", "sid", "idle")
    assert worker.process_improves(improve) == 1
    # Another turn lands inside the 90-minute cooldown: no second billed improve.
    common.bump_turn_counter("sid")
    path = worker.request_improve("ds", "sid", "idle")
    assert worker.process_improves(improve) == 0
    assert worker.read(path)["next_attempt"] > worker.time.time() + 3600
    # Explicit requests (session end, manual sync, switch) are never throttled.
    worker.request_improve("ds", "sid", "final")
    assert worker.process_improves(improve) == 1
    # After the cooldown, nothing new since the last improve: drop the request.
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "0")
    path = worker.request_improve("ds", "sid", "idle")
    assert worker.process_improves(improve) == 0 and not path.exists()
    common.bump_turn_counter("sid")
    worker.request_improve("ds", "sid", "idle")
    assert worker.process_improves(improve) == 1
    assert calls == ["idle", "final", "idle"]


def test_timed_out_improve_is_not_resubmitted_soon_and_is_capped(worker, monkeypatch):
    monkeypatch.delenv("COGNEE_IMPROVE_SUBMIT_TIMEOUT", raising=False)
    assert worker.improve_submit_timeout() == 420.0
    path = worker.request_improve("ds", "sid", "manual")
    calls = []

    def slow(item):
        calls.append(item)
        raise TimeoutError("The read operation timed out")

    now = worker.time.time()
    for attempt in range(1, worker.MAX_IMPROVE_FAILURES):
        assert worker.process_improves(slow, now=now) == 0
        due = worker.read(path)["next_attempt"]
        assert due >= now + 5400
        assert worker.process_improves(slow, now=due - 1) == 0  # nothing resubmitted early
        now = due
    assert worker.process_improves(slow, now=now) == 0
    assert len(calls) == worker.MAX_IMPROVE_FAILURES
    assert not path.exists() and (path.parent / "expired" / path.name).exists()


def test_delivered_capture_is_never_written_back_when_bookkeeping_fails(worker, monkeypatch):
    path = worker.enqueue("ds", "sid", {"type": "qa", "answer": "once"}, "answer")

    def busy(*a, **kw):
        raise OSError("improve request busy")

    monkeypatch.setattr(worker, "request_improve", busy)
    sent = []
    assert worker.drain(lambda item: sent.append(item) or {"entry_id": "saved"}) == 1
    assert not path.exists() and not list((worker.root() / "spool").glob("*.json"))
    assert worker.drain(lambda item: sent.append(item) or {"entry_id": "saved"}) == 0
    assert len(sent) == 1
    stats = json.loads((worker.session_dir("host-a") / "stats.json").read_text())
    assert stats["saves"] == 1


def test_one_improve_per_pass_with_explicit_requests_first(worker):
    worker.request_improve("ds", "idle-session", "idle")
    worker.request_improve("ds", "switching", "switch")
    seen = []

    def improve(item):
        seen.append(item["session_id"])
        return COMPLETED

    assert worker.process_improves(improve) == 1
    assert seen == ["switching"]
    assert worker.process_improves(improve) == 1
    assert seen == ["switching", "idle-session"]


def test_cloud_improve_triggers_every_n_delivered_entries_like_local(worker, monkeypatch):
    # SDK-1072/1074: no improve after every turn or quiet pause; one every
    # COGNEE_AUTO_IMPROVE_EVERY stored entries, within the cooldown.
    monkeypatch.setenv("COGNEE_AUTO_IMPROVE_EVERY", "3")
    wanted = worker.root() / "improve-wanted"
    for i in range(2):
        worker.enqueue("ds", "sid", {"type": "qa", "answer": str(i)}, "answer")
    worker.drain(lambda item: {"entry_id": "saved"})
    assert not list(wanted.glob("*.json"))
    worker.enqueue("ds", "sid", {"type": "qa", "answer": "third"}, "answer")
    worker.drain(lambda item: {"entry_id": "saved"})
    (marker,) = wanted.glob("*.json")
    assert worker.read(marker)["reason"] == "turn_3"
    # Not explicit, and nothing is queued for the session: it runs without a quiet wait.
    assert worker.process_improves(lambda _: COMPLETED) == 1


@pytest.mark.parametrize("reason", ["final", "precompact"])
def test_final_and_precompact_skip_when_nothing_new_was_stored(worker, reason):
    import _plugin_common as common

    common.bump_turn_counter("sid")
    worker.request_improve("ds", "sid", "precompact")
    assert worker.process_improves(lambda _: COMPLETED) == 1
    # Compacted, then ended with nothing stored in between: no second improve.
    path = worker.request_improve("ds", "sid", reason)
    assert worker.process_improves(lambda _: pytest.fail("nothing new to improve")) == 0
    assert not path.exists()
    common.bump_turn_counter("sid")
    worker.request_improve("ds", "sid", reason)
    assert worker.process_improves(lambda _: COMPLETED) == 1


@pytest.mark.parametrize("reason", ["manual", "switch"])
def test_manual_and_switch_always_run(worker, reason):
    import _plugin_common as common

    common.bump_turn_counter("sid")
    worker.request_improve("ds", "sid", "final")
    assert worker.process_improves(lambda _: COMPLETED) == 1
    worker.request_improve("ds", "sid", reason)
    assert worker.process_improves(lambda _: COMPLETED) == 1


def test_improve_runs_in_its_own_pass_so_the_main_loop_never_blocks(worker):
    started = []
    # Nothing due: no child process is started.
    assert not worker.start_improve_pass(spawn=lambda: started.append(1))
    worker.request_improve("ds", "sid", "manual")
    assert worker.start_improve_pass(spawn=lambda: started.append(1)) and started == [1]
    # While a pass holds the lock, the loop starts no second one and a second
    # pass process exits without submitting.
    with worker.file_lock(worker.root() / "improve-pass.lock") as held:
        assert held
        assert not worker.start_improve_pass(spawn=lambda: started.append(2))
        assert worker.improve_pass(lambda _: pytest.fail("pass already running")) == 0
    assert started == [1]
    assert worker.improve_pass(lambda _: COMPLETED) == 1


def test_main_loop_never_submits_an_improve_itself():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3] / "claude-code/scripts/_cloud_worker.py"
    ).read_text()
    loop = source[source.index("def run() -> None:") :]
    assert "process_improves(" not in loop and "start_improve_pass()" in loop


def test_request_arriving_during_an_improve_is_throttled_not_resubmitted(worker):
    import _plugin_common as common

    common.bump_turn_counter("sid")
    worker.request_improve("ds", "sid", "turn_150")
    calls = []

    def improve(item):
        calls.append(item["reason"])
        # The session keeps working while the improve blocks for minutes.
        common.bump_turn_counter("sid")
        worker.request_improve("ds", "sid", "turn_300")
        return COMPLETED

    assert worker.process_improves(improve) == 1
    assert common.read_improve_state("sid")["last_improved_at"] > 0
    # The newer automatic request waits for the cooldown instead of paying again.
    assert worker.process_improves(improve) == 0
    assert calls == ["turn_150"]
    # Entries stored during the improve are still new for the session-end one.
    worker.request_improve("ds", "sid", "final")
    assert worker.process_improves(lambda _: COMPLETED) == 1


@pytest.mark.parametrize(
    "result",
    [
        {"stages": [{"status": "errored"}]},
        {"stages": [{"status": "mystery"}, {"status": "completed"}]},
        {"ok": True},
    ],
)
def test_billable_failures_are_capped(worker, result):
    path = worker.request_improve("ds", "sid", "manual")
    now = worker.time.time()
    calls = []
    for _ in range(worker.MAX_IMPROVE_FAILURES):
        worker.process_improves(lambda _: calls.append(1) or result, now=now)
        if path.exists():
            now = worker.read(path)["next_attempt"] + 1
    assert len(calls) == worker.MAX_IMPROVE_FAILURES
    assert not path.exists() and (path.parent / "expired" / path.name).exists()
    assert worker.process_improves(lambda _: pytest.fail("archived")) == 0


def test_skipped_improve_is_terminal(worker):
    path = worker.request_improve("ds", "sid", "manual")
    skipped = {"stages": [{"status": "skipped", "reason": "nothing_to_do"}]}
    assert worker.process_improves(lambda _: skipped) == 0
    assert not path.exists() and (path.parent / "expired" / path.name).exists()


def test_unreachable_server_is_retried_without_using_up_attempts(worker):
    import urllib.error

    path = worker.request_improve("ds", "sid", "final")
    now = worker.time.time()

    def offline(_):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    for _ in range(worker.MAX_IMPROVE_FAILURES + 2):
        worker.process_improves(offline, now=now)
        item = worker.read(path)
        assert item["last_error"] == "unreachable" and item.get("failures", 0) == 0
        now = item["next_attempt"] + 1
    assert worker.process_improves(lambda _: COMPLETED, now=now) == 1


# ── the status row hears about spooled captures ────────────────────────────


def _saved(host_key: str) -> dict:
    pc = __import__("_plugin_common")
    return pc.phase_snapshot(session_key=host_key).get("results", {}).get("saved", {})


def test_a_delivered_capture_is_reported_as_saved(worker):
    """The Stop hook only spools in cloud mode, so the band's saving… had no
    end until the worker said so."""
    path = worker.enqueue("ds", "sid", {"type": "qa", "answer": "a"}, "answer")
    worker.drain(lambda item: {"entry_id": "e1"}, now=worker.read(path)["created_at"])
    saved = _saved("host-a")
    assert saved.get("ok") is True and saved.get("count") == 1


def test_a_capture_held_for_retry_is_reported_as_queued(worker):
    path = worker.enqueue("ds", "sid", {"type": "qa", "answer": "a"}, "answer")

    def offline(item):
        raise ConnectionError("down")

    worker.drain(offline, now=worker.read(path)["created_at"])
    saved = _saved("host-a")
    assert saved.get("ok") is False and saved.get("buffered") is True


def test_a_rejected_capture_is_reported_as_failed(worker, monkeypatch):
    import urllib.error

    path = worker.enqueue("ds", "sid", {"type": "qa", "answer": "a"}, "answer")

    def rejected(item):
        raise urllib.error.HTTPError("u", 422, "Unprocessable", {}, None)

    monkeypatch.setattr(worker, "PERMANENT_REJECTIONS", {422})
    worker.drain(rejected, now=worker.read(path)["created_at"])
    saved = _saved("host-a")
    assert saved.get("ok") is False and "buffered" not in saved


# ── the worker follows ~/.cognee/.env ──────────────────────────────────────


def test_target_follows_an_edit_of_the_env_file(suite, isolated_modules, monkeypatch, tmp_path):
    """The worker lives for hours. Pinned to the tenant it started with, it
    never served a session pointed at another, whose captures and improves
    then starved (a dataset switch waited its full 14 minutes on one)."""
    if suite.name != "claude-code":
        pytest.skip("Cloud worker")
    # The loader scrubs COGNEE_* before the import, so the file is named
    # afterwards: exactly a worker that started before the file was edited.
    worker = isolated_modules(suite, "_cloud_worker")
    monkeypatch.setattr(worker, "ensure_worker", lambda: None)
    env_file = tmp_path / "cognee.env"
    monkeypatch.setenv("COGNEE_ENV_FILE", str(env_file))
    env_file.write_text('COGNEE_BASE_URL="https://tenant-a.aws.cognee.ai"\n', encoding="utf-8")
    assert worker.target()["base_url"] == "https://tenant-a.aws.cognee.ai"

    env_file.write_text('COGNEE_BASE_URL="https://tenant-b.stg-aws.cognee.ai"\n', encoding="utf-8")
    assert worker.target()["base_url"] == "https://tenant-b.stg-aws.cognee.ai"

    # A real export still wins over the file, as at load.
    monkeypatch.setenv("COGNEE_BASE_URL", "https://exported.example")
    env_file.write_text('COGNEE_BASE_URL="https://tenant-c.aws.cognee.ai"\n', encoding="utf-8")
    assert worker.target()["base_url"] == "https://exported.example"
