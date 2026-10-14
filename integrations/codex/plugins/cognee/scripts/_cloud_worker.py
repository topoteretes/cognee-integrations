"""Durable cloud capture spool and one per-user worker. Hooks perform file I/O only."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from _file_lock import file_lock
from _injection_ledger import atomic_json, session_dir

MAX_AGE = 7 * 86400
EXPLICIT_IMPROVES = {"final", "precompact", "manual", "switch"}


def enabled(runtime: dict = None) -> bool:
    from _plugin_common import _local_api_url, service_url_is_local
    from config import load_config

    config = load_config()
    if config.get("_forced_backend") == "local":
        return False
    if config.get("_forced_backend") == "cloud":
        return True
    url = (runtime or {"base_url": _local_api_url()}).get("base_url", "")
    return bool(url) and not service_url_is_local(url)


def root() -> Path:
    return Path.home() / ".cognee-plugin" / "codex"


def read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def log(event: str, **detail) -> None:
    from _plugin_common import hook_log

    hook_log(event, detail)


def target() -> dict:
    from _plugin_common import _local_api_url, load_cached_api_key
    from config import load_config

    url = _local_api_url().rstrip("/")
    key = (
        os.environ.get("COGNEE_PRINCIPAL_API_KEY")
        or load_config().get("api_key")
        or load_cached_api_key(url)
        or ""
    )
    return {"base_url": url, "principal": hashlib.sha256(key.encode()).hexdigest()}


def ensure_worker() -> None:
    """Re-resolve the installed script every launch; the running worker adopts updates."""
    if os.environ.get("COGNEE_IDLE_DISABLED", "").lower() in {"1", "true", "yes"}:
        return
    state = root()
    state.mkdir(parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())
    atomic_json(state / "cloud-worker-runtime.json", {"script": script, "python": sys.executable})
    from _proc import pid_alive

    pid = read(state / "cloud-worker-pid.json").get("pid", 0)
    if pid and pid_alive(pid):
        return
    # The worker owns a lifetime file lock, so simultaneous spawns are harmless.
    from _logfiles import rotate_if_oversized

    rotate_if_oversized(state / "cloud-worker.log")
    with (state / "cloud-worker.log").open("a", encoding="utf-8") as stream:
        subprocess.Popen(
            [sys.executable, script],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
            close_fds=True,
        )


def _refresh_console_log() -> None:
    """Rotate and reopen the detached worker's inherited stdout/stderr handles."""
    from _logfiles import rotate_if_oversized

    path = root() / "cloud-worker.log"
    sys.stdout.flush()
    sys.stderr.flush()
    rotate_if_oversized(path)
    with path.open("a", encoding="utf-8") as stream:
        os.dup2(stream.fileno(), 1)
        os.dup2(stream.fileno(), 2)


def register_session(bootstrap: dict) -> None:
    key = bootstrap["session_key"]
    path = session_dir(key) / "worker.json"
    with file_lock(path.with_suffix(".lock"), timeout=0.2) as acquired:
        if not acquired:
            raise OSError("cloud session state busy")
        previous = read(path)
        identity = target()
        same = (
            previous.get("target") == identity
            and previous.get("session_id") == bootstrap["session_id"]
        )
        atomic_json(
            path,
            {
                **bootstrap,
                "target": identity,
                "activity_at": time.time(),
                "created_at": time.time(),
                "registered": bool(
                    same and previous.get("registered") and not previous.get("unregistered")
                ),
                "next_attempt": 0,
                "attempts": 0,
            },
        )
    ensure_worker()
    from _session_stats import update

    update(key, metadata={"ended_at": None})


def touch_session() -> None:
    from _plugin_common import get_session_key

    key = get_session_key()
    if not key:
        return
    path = session_dir(key) / "worker.json"
    with file_lock(path.with_suffix(".lock"), timeout=0.2) as acquired:
        if acquired:
            state = read(path)
            if state:
                state["activity_at"] = time.time()
                atomic_json(path, state)
    ensure_worker()


def enqueue(dataset: str, session_id: str, entry: dict, kind: str) -> Path:
    from _capture_policy import redact
    from _plugin_common import _sanitize_value, get_session_key

    key = get_session_key()
    session_dir(key)  # validate before constructing a filename
    created = time.time()
    item = {
        "dataset": dataset,
        "session_id": session_id,
        "host_key": key,
        "entry": _sanitize_value(redact(entry)),
        "kind": kind,
        "target": target(),
        "created_at": created,
        "attempts": 0,
        "next_attempt": 0,
        "ambiguous": False,
        "client_event_id": str(uuid.uuid4()),
    }
    path = root() / "spool" / f"{time.time_ns():020d}-{key}-{uuid.uuid4().hex}.json"
    atomic_json(path, item)
    log("capture_spooled", session=session_id, kind=kind)
    touch_session()
    return path


def request_improve(dataset: str, session_id: str, reason: str = "stop") -> Path:
    from _plugin_common import get_session_key

    key = get_session_key()
    session_dir(key)
    identity = target()
    token = hashlib.sha256(
        json.dumps([identity, dataset, session_id], sort_keys=True).encode()
    ).hexdigest()
    path = root() / "improve-wanted" / (token + ".json")
    with file_lock(path.with_suffix(".lock"), timeout=0.2) as acquired:
        if not acquired:
            raise OSError("improve request busy")
        old = read(path)
        now = time.time()
        explicit = reason in EXPLICIT_IMPROVES
        atomic_json(
            path,
            {
                "dataset": dataset,
                "session_id": session_id,
                "host_key": key,
                "target": identity,
                "reason": (
                    reason
                    if explicit
                    else old.get("reason")
                    if old.get("reason") in EXPLICIT_IMPROVES
                    else reason
                ),
                "generation": uuid.uuid4().hex,
                "created_at": now if explicit else old.get("created_at", now),
                "attempts": 0 if explicit else old.get("attempts", 0),
                "next_attempt": 0 if explicit else old.get("next_attempt", 0),
                "last_error": old.get("last_error", ""),
                "requested_at": now,
            },
        )
    ensure_worker()
    return path


def improve_wait_detail(path: Path, *, now: float = None) -> str:
    item = read(path)
    now = time.time() if now is None else now
    error = str(item.get("last_error") or "pending").replace("\n", " ")[:200]
    remaining = max(0, int(item.get("next_attempt", 0) - now))
    return f"Last result: {error}; next retry in {remaining}s."


def _expire(path: Path, kind: str) -> None:
    expired = path.parent / "expired"
    expired.mkdir(parents=True, exist_ok=True)
    os.replace(path, expired / path.name)
    log("cloud_work_expired", kind=kind, file=path.name, retention_days=7)


def _retry(path: Path, item: dict, now: float, error: str, *, retry_after=None) -> None:
    item["attempts"] = int(item.get("attempts", 0)) + (retry_after is None)
    delay = (
        retry_after
        if retry_after is not None
        else min(3600, 5 * 2 ** min(item["attempts"] - 1, 10))
    )
    item["next_attempt"] = now + delay
    item["last_error"] = error[:200]
    atomic_json(path, item)
    log("cloud_work_retry", file=path.name, attempts=item["attempts"], error=error[:200])


def drain(send, *, now: float = None, verify=None, prepare=None) -> int:
    """Ordered, bounded replay. Keep failures, quarantine malformed/expired events."""
    from _capture_policy import allow_tool, capture_enabled, redact

    if not capture_enabled():
        return 0
    now = time.time() if now is None else now
    deadline = time.monotonic() + 20
    delivered = 0
    identity = target()
    for path in sorted((root() / "spool").glob("*.json")):
        if time.monotonic() >= deadline:
            break
        item = read(path)
        if not item or now - item.get("created_at", 0) > MAX_AGE:
            _expire(path, "capture")
            continue
        if item.get("target") != identity:
            continue
        if now < item.get("next_attempt", 0):
            break
        try:
            if item.get("kind") == "trace" and not allow_tool(
                item.get("entry", {}).get("origin_function", ""),
                item.get("entry", {}).get("method_params", {}),
            ):
                _expire(path, "capture_disabled_by_policy")
                continue
            redacted = redact(item["entry"])
            if item.get("request") and redacted != item["entry"]:
                # Never change a payload after its event ID may have committed.
                _expire(path, "capture_policy_changed")
                continue
            item["entry"] = redacted
            item.setdefault("client_event_id", str(uuid.uuid4()))
            if prepare and not item.get("request"):
                item.update(prepare(item))
            already_stored = bool(
                not item.get("idempotent") and item.get("ambiguous") and verify and verify(item)
            )
            if not already_stored:
                # Write before POST: an interrupted worker must verify before replay.
                item["ambiguous"] = True
                atomic_json(path, item)
                result = send(item)
                if not isinstance(result, dict) or not (
                    result.get("entry_id") or result.get("ok") is True
                ):
                    raise RuntimeError("capture response did not confirm acceptance")
                if item.get("idempotent") and (
                    result.get("client_event_id") != item["client_event_id"]
                    or not result.get("entry_id")
                    or not result.get("payload_hash")
                ):
                    raise RuntimeError("capture response did not confirm the event receipt")
            from _plugin_common import bump_turn_counter, clear_payment_required
            from _session_stats import update

            update(item["host_key"], saves=1, metadata={"session_id": item["session_id"]})
            bump_turn_counter(item["session_id"])
            clear_payment_required()
            path.unlink()
            log(
                "trace_stored" if item["kind"] == "trace" else "stop_stored",
                session=item["session_id"],
                via="cloud_spool",
            )
            delivered += 1
            request_improve(item["dataset"], item["session_id"], "idle")
            log(
                "cloud_capture_delivered",
                session=item["session_id"],
                kind=item["kind"],
                verified=already_stored,
            )
        except Exception as exc:
            if getattr(exc, "code", None) == 409 and item.get("idempotent"):
                try:
                    conflict = json.loads(exc.read()).get("code") == "capture_payload_conflict"
                except (AttributeError, ValueError, OSError):
                    conflict = False
                if conflict:
                    _expire(path, "capture_payload_conflict")
                    continue
            if getattr(exc, "code", None) == 402:
                from _plugin_common import record_payment_required

                record_payment_required("save")
            _retry(path, item, now, type(exc).__name__ + ": " + str(exc)[:160])
            break
    return delivered


def improve_outcome(result) -> dict:
    """Decode ImproveResult; a successful HTTP submit alone never proves completion."""
    if not isinstance(result, dict) or not result:
        return {"ok": False, "reason": "busy" if result == {} else "unknown_result"}
    stages = result.get("stages")
    if not isinstance(stages, list) or not stages:
        return {"ok": False, "reason": "unsupported_result"}
    stages = stages + [s for run in result.get("rerun_passes", []) for s in run]
    if result.get("status") == "running" or result.get("finished") is False:
        return {"ok": False, "reason": "running"}
    if result.get("error") or any(s.get("status") == "errored" for s in stages):
        return {"ok": False, "reason": "errored"}
    if any(s.get("reason") == "lock_held" for s in stages):
        return {"ok": False, "reason": "busy"}
    if not any(s.get("status") in {"completed", "already_completed"} for s in stages):
        return {"ok": False, "reason": "skipped"}
    if any(s.get("status") not in {"completed", "already_completed", "skipped"} for s in stages):
        return {"ok": False, "reason": "unknown_stage"}
    added = sum(
        max(0, int(stage.get("counts", {}).get("lessons", 0)))
        for stage in stages
        if stage.get("stage") == "extract_agent_context"
    )
    return {"ok": True, "reason": "completed", "distillation_entries": added}


def process_improves(improve, *, now: float = None) -> int:
    now = time.time() if now is None else now
    done = 0
    identity = target()
    pending_sessions = {read(p).get("session_id") for p in (root() / "spool").glob("*.json")}
    for path in sorted((root() / "improve-wanted").glob("*.json")):
        item = read(path)
        if not item or now - item.get("created_at", 0) > MAX_AGE:
            _expire(path, "improve")
            continue
        if item.get("target") != identity or now < item.get("next_attempt", 0):
            continue
        if item["session_id"] in pending_sessions:
            continue
        state = read(session_dir(item["host_key"]) / "worker.json")
        idle = now - state.get("activity_at", 0)
        if item["reason"] not in {"final", "precompact", "manual", "switch"} and idle < 60:
            continue
        try:
            outcome = improve_outcome(improve(item))
        except Exception as exc:
            outcome = {"ok": False, "reason": type(exc).__name__}
        with file_lock(path.with_suffix(".lock"), timeout=0.2) as acquired:
            if not acquired:
                continue
            current = read(path)
            if current.get("generation") != item.get("generation"):
                continue  # a newer request must not be acknowledged or overwritten
            if outcome.get("ok"):
                from _session_stats import update

                update(
                    item["host_key"],
                    improves_landed=1,
                    distillation_entries=outcome.get("distillation_entries", 0),
                )
                path.unlink()
                atomic_json(
                    session_dir(item["host_key"]) / "last-improve.json",
                    {**outcome, "at": now, "session_id": item["session_id"]},
                )
                log(
                    "improve_fired",
                    session=item["session_id"],
                    dataset=item["dataset"],
                    trigger=item["reason"],
                    ok=True,
                    reason="completed",
                )
                done += 1
                from _context_cache import request_refresh

                request_refresh(item["dataset"], item["session_id"], item["host_key"])
            else:
                reason = outcome.get("reason", "unconfirmed")
                _retry(
                    path,
                    item,
                    now,
                    reason,
                    retry_after=5 if reason in {"busy", "running"} else None,
                )
                log(
                    "improve_fired",
                    session=item["session_id"],
                    dataset=item["dataset"],
                    trigger=item["reason"],
                    ok=False,
                    reason=outcome.get("reason"),
                )
    return done


def _bootstrap_sessions() -> None:
    from _plugin_common import set_session_key
    from config import load_config

    identity = target()
    for path in (root() / "sessions").glob("*/worker.json"):
        state = read(path)
        if (
            state.get("target") != identity
            or state.get("registered")
            or time.time() - state.get("created_at", time.time()) > MAX_AGE
        ):
            continue
        now = time.time()
        if now < state.get("next_attempt", 0):
            continue
        try:
            set_session_key(state["session_key"])
            spec = importlib.util.spec_from_file_location(
                "cloud_session_start", Path(__file__).with_name("session-start.py")
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = asyncio.run(
                module._run_heavy(
                    load_config(),
                    state.get("cwd", ""),
                    state["session_id"],
                    state["agent_session_name"],
                    state["session_key"],
                    state["dataset"],
                    managed_endpoint=True,
                    boot_timeout=4,
                )
            )
            if not result[2]:
                raise RuntimeError("cloud registration incomplete")
            with file_lock(path.with_suffix(".lock"), timeout=0.2) as acquired:
                if acquired:
                    current = read(path)
                    if current.get("session_id") == state["session_id"]:
                        current["registered"] = True
                        atomic_json(path, current)
        except Exception as exc:
            with file_lock(path.with_suffix(".lock"), timeout=0.2) as acquired:
                if acquired:
                    current = read(path)
                    if current.get("session_id") == state["session_id"]:
                        _retry(path, current, now, type(exc).__name__)


def maintain_sessions() -> None:
    """Finish abandoned launches and recover buffers written by older versions."""
    from _plugin_common import (
        _read_map_record,
        drain_warmup_entries,
        set_session_key,
        unregister_agent_via_http,
    )
    from _proc import pid_alive
    from _session_stats import read as read_stats
    from _session_stats import update

    identity = target()
    for path in (root() / "sessions").glob("*/worker.json"):
        state = read(path)
        if state.get("target") != identity:
            continue
        key = state["session_key"]
        set_session_key(key)
        record = _read_map_record(key)
        sid, dataset = state["session_id"], state["dataset"]
        drained, _ = drain_warmup_entries(dataset, sid, budget_seconds=4)
        if drained:
            request_improve(dataset, sid, "idle")
        ended = read_stats(key).get("ended_at")
        pid = record.get("host_pid", 0)
        if not ended and pid and not pid_alive(pid):
            update(key, metadata={"ended_at": time.time()})
            request_improve(dataset, sid, "final")
            ended = True
        if ended and state.get("registered") and not state.get("unregistered"):
            ok, _ = unregister_agent_via_http(
                agent_session_name=state["agent_session_name"], timeout=4
            )
            if ok:
                with file_lock(path.with_suffix(".lock"), timeout=0.2) as acquired:
                    if acquired:
                        current = read(path)
                        current["unregistered"] = True
                        atomic_json(path, current)


def run() -> None:
    from _plugin_common import (
        DEFINITIVE_FAILURE_STATES,
        _entry_fingerprint,
        _json_http_request,
        _server_session_fingerprints,
        authed_liveness,
        dataset_id_for,
        get_session_detail_via_http,
        mark_server_ready,
        prepare_remember_entry_payload,
        refresh_credits,
        require_typed_dataset_id_support,
        set_session_key,
        write_connection_state,
        write_fields,
    )

    state = root()
    with file_lock(state / "cloud-worker.lock") as acquired:
        if not acquired:
            return
        atomic_json(state / "cloud-worker-pid.json", {"pid": os.getpid()})
        from _plugin_common import sweep_stale_state

        sweep_stale_state()
        _refresh_console_log()
        last_sweep = time.time()
        last_health = 0.0
        capture_capability = {"checked_at": 0.0, "target": None, "supported": False}

        def prepare(item):
            set_session_key(item["host_key"])
            current_target = target()
            if (
                capture_capability["target"] != current_target
                or time.monotonic() - capture_capability["checked_at"] > 60
            ):
                health = _json_http_request("/health", method="GET", timeout=4)
                capture_capability.update(
                    checked_at=time.monotonic(),
                    target=current_target,
                    supported="capture_idempotency_v1" in (health or {}).get("capabilities", []),
                )
            # An ambiguous legacy POST cannot acquire a new idempotency key retroactively.
            idempotent = capture_capability["supported"] and not item.get("ambiguous")
            payload = prepare_remember_entry_payload(
                item["dataset"], item["session_id"], item["entry"]
            )
            if idempotent:
                payload["client_event_id"] = item["client_event_id"]
            elif payload.get("dataset_id"):
                require_typed_dataset_id_support()
            return {"request": payload, "idempotent": idempotent}

        def send(item):
            set_session_key(item["host_key"])
            return _json_http_request("/api/v1/remember/entry", item["request"], timeout=10)

        def verify(item):
            set_session_key(item["host_key"])
            detail = get_session_detail_via_http(item["session_id"], timeout=4)
            return bool(
                detail and _entry_fingerprint(item["entry"]) in _server_session_fingerprints(detail)
            )

        def improve(item):
            set_session_key(item["host_key"])
            from _project_memory import route

            dataset = route(item["dataset"], item["session_id"])["write"]
            fields = write_fields(dataset_id_for(dataset) or dataset)
            return _json_http_request(
                "/api/v1/improve",
                {
                    **fields,
                    "session_ids": [item["session_id"]],
                    "run_in_background": False,
                },
                timeout=180,
            )

        while True:
            desired = read(state / "cloud-worker-runtime.json")
            if desired.get("script") and desired["script"] != str(Path(__file__).resolve()):
                os.execv(desired["python"], [desired["python"], desired["script"]])
            try:
                _bootstrap_sessions()
                if time.time() - last_health >= 20:
                    last_health = time.time()
                    if last_health - last_sweep >= 3600:
                        sweep_stale_state()
                        last_sweep = last_health
                    _refresh_console_log()
                    url = target()["base_url"]
                    health = authed_liveness(url, timeout=2)
                    identity = target()
                    for launch in (root() / "sessions").glob("*/worker.json"):
                        record = read(launch)
                        if record.get("target") != identity:
                            continue
                        set_session_key(record["session_key"])
                        if health == "ready":
                            mark_server_ready(url)
                        elif health in DEFINITIVE_FAILURE_STATES:
                            write_connection_state(health, url)
                    refresh_credits()
                    maintain_sessions()
                drain(send, verify=verify, prepare=prepare)
                process_improves(improve)
                from _context_cache import refresh_pending, schedule_active_refreshes

                schedule_active_refreshes()
                refresh_pending()
            except Exception as exc:
                log("cloud_worker_error", error=type(exc).__name__)
            time.sleep(2)


if __name__ == "__main__":
    run()
