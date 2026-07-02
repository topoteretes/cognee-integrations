#!/usr/bin/env python3
"""Switch the active dataset mid-session without dropping conversation context.

A dataset switch keeps the SAME Cognee ``session_id`` (and the ``conn_uuid``
registration handle), so the session cache — which is keyed by ``session_id``,
not the dataset — is untouched and ``recall`` still returns prior-conversation
context after the switch. Only *where new graph writes land* changes.

Three phases, ordered so a crash mid-switch never orphans or duplicates state:

  1. Seal the old ``(dataset, session_id)`` bridge: flush its buffered QA/trace
     to the old dataset's graph (else the session-end sync, which resolves only
     the *current* dataset, would never flush it), then mark it sealed.
  2. Switch the active dataset (persist it so every later hook keys under the new
     dataset) and seed the new dataset's high-water baseline from the sealed
     counts, so the local-SDK bridge re-emits only post-switch turns.
  3. Re-register the agent against the new dataset with the SAME
     ``agent_session_name`` (conn_uuid) + ``session_id`` — an in-place
     re-register, not a fresh connection.

Idempotent: switching to the dataset already active is a no-op. Best-effort:
failures log and degrade; they never crash the calling hook.

Invocation (any of):
  * ``python dataset-switch.py <new_dataset>``
  * ``COGNEE_SWITCH_DATASET=<new_dataset> python dataset-switch.py``
  * a hook payload on stdin carrying ``new_dataset`` / ``dataset``
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    get_session_key,
    hook_log,
    http_api_ready,
    register_agent_via_http,
    resolve_cognee_session_id,
    resolve_conn_uuid,
    resolve_session_key_from_payload,
    resolve_user,
    resolved_http_endpoint_auth,
    seal_bridge_state,
    set_active_dataset_for_session,
    set_dataset_baseline,
    set_session_key,
)
from config import (
    ensure_cognee_ready,
    ensure_dataset_ready,
    get_dataset,
    load_config,
    seal_session_bridge_local,
    set_active_dataset,
)


def _resolve_new_dataset(payload: dict, argv: list[str]) -> str:
    """Resolve the requested new dataset from CLI arg, env, or hook payload."""
    for arg in argv[1:]:
        text = str(arg or "").strip()
        if text and not text.startswith("-"):
            return text
    env = str(os.environ.get("COGNEE_SWITCH_DATASET", "") or "").strip()
    if env:
        return env
    if isinstance(payload, dict):
        for key in ("new_dataset", "dataset", "datasetName", "dataset_name"):
            val = str(payload.get(key) or "").strip()
            if val:
                return val
    return ""


async def switch_dataset(new_dataset: str, cwd: str = "") -> dict:
    """Orchestrate the three-phase switch. Returns a structured result dict."""
    new_dataset = str(new_dataset or "").strip()
    session_key = set_session_key(get_session_key())
    session_id = resolve_cognee_session_id(session_key, cwd)
    conn_uuid = resolve_conn_uuid(session_key)
    config = load_config()
    old_dataset = str(get_dataset(config) or "").strip()

    if not new_dataset:
        hook_log("dataset_switch_noop", {"reason": "no_new_dataset", "session": session_id})
        return {"status": "noop", "reason": "no_new_dataset"}
    if not session_id:
        hook_log("dataset_switch_noop", {"reason": "no_session_id"})
        return {"status": "noop", "reason": "no_session_id"}
    if new_dataset == old_dataset:
        hook_log(
            "dataset_switch_noop",
            {"reason": "already_active", "dataset": new_dataset, "session": session_id},
        )
        return {"status": "noop", "reason": "already_active", "dataset": new_dataset}

    hook_log(
        "dataset_switch_requested",
        {"old_dataset": old_dataset, "new_dataset": new_dataset, "session": session_id},
    )

    api_mode = http_api_ready()

    # Phase 1: seal the old bridge (flush old dataset, mark sealed, high-water).
    if api_mode:
        seal = seal_bridge_state(old_dataset, session_id)
    else:
        config_local = load_config()
        try:
            await ensure_cognee_ready(config_local)
        except Exception as exc:
            hook_log("dataset_switch_local_ready_failed", {"error": str(exc)[:200]})
        user = await resolve_user("")
        try:
            await ensure_dataset_ready(old_dataset, user)
        except Exception as exc:
            hook_log("dataset_switch_local_dataset_ready_failed", {"error": str(exc)[:200]})
        seal = await seal_session_bridge_local(old_dataset, session_id, user)

    # Phase 2: switch the active dataset + seed the new dataset's baseline so it
    # only ever receives post-switch content (no duplicate graph writes).
    stores = set_active_dataset(new_dataset, cwd)
    set_dataset_baseline(
        session_id,
        new_dataset,
        int(seal.get("qa_count", 0) or 0),
        int(seal.get("trace_count", 0) or 0),
    )
    set_active_dataset_for_session(session_id, new_dataset)

    # Phase 3: re-register the agent in place (same conn_uuid + session_id).
    reregistered = False
    if api_mode:
        resolved_http_endpoint_auth()
        reregistered, registration = register_agent_via_http(
            agent_session_name=conn_uuid,
            session_id=session_id,
            dataset_names=[new_dataset],
        )
        hook_log(
            "dataset_switch_agent_reregistered",
            {
                "message": "agent re-registered",
                "agent_session_name": conn_uuid,
                "session": session_id,
                "new_dataset": new_dataset,
                "ok": reregistered,
                "connection_id": str(registration.get("id", "")) if registration else "",
            },
        )
    else:
        # Local SDK mode has no server-side agent registry to re-register against.
        hook_log(
            "dataset_switch_agent_reregister_skipped",
            {"reason": "local_sdk_mode", "session": session_id, "new_dataset": new_dataset},
        )

    hook_log(
        "dataset_switch_complete",
        {
            "old_dataset": old_dataset,
            "new_dataset": new_dataset,
            "session": session_id,
            "mode": "http" if api_mode else "local_sdk",
            "sealed": seal.get("sealed", False),
            "flushed": seal.get("flushed", False),
            "reregistered": reregistered,
            "stores": stores,
        },
    )
    return {
        "status": "switched",
        "old_dataset": old_dataset,
        "new_dataset": new_dataset,
        "session_id": session_id,
        "mode": "http" if api_mode else "local_sdk",
        "seal": seal,
        "reregistered": reregistered,
    }


def main() -> None:
    payload: dict = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
        except Exception:
            raw = ""
        if raw.strip():
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
    if isinstance(payload, dict) and payload:
        session_key_candidate, _ = resolve_session_key_from_payload(payload)
        if session_key_candidate:
            set_session_key(session_key_candidate)

    new_dataset = _resolve_new_dataset(payload, sys.argv)
    cwd = str(
        (payload.get("cwd") if isinstance(payload, dict) else "")
        or os.environ.get("CLAUDE_CWD")
        or os.getcwd()
    )

    try:
        result = asyncio.run(switch_dataset(new_dataset, cwd))
    except Exception as exc:
        hook_log("dataset_switch_failed", {"error": str(exc)[:300]})
        print(f"cognee-dataset-switch: failed ({exc})", file=sys.stderr)
        return

    status = result.get("status")
    if status == "switched":
        print(
            f"cognee-dataset-switch: {result.get('old_dataset')} -> "
            f"{result.get('new_dataset')} (session={result.get('session_id')})",
            file=sys.stderr,
        )
    else:
        print(f"cognee-dataset-switch: no-op ({result.get('reason')})", file=sys.stderr)


if __name__ == "__main__":
    main()
