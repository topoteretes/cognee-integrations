#!/usr/bin/env python3
"""Store the user's prompt into the Cognee session cache as a QAEntry.

Runs async on the UserPromptSubmit hook so it doesn't block the
parallel context-lookup hook.

Configuration:
    Uses resolved session ID from SessionStart hook (via ~/.cognee-plugin/resolved.json).
"""

import asyncio
import json
import os
import sys

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import hook_log, load_resolved, notify, resolve_user, touch_activity
from config import ensure_cognee_ready, get_dataset, get_session_id, load_config

MAX_TEXT = 4000


def _load_session() -> tuple[str, str, str]:
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    dataset = resolved.get("dataset", "")
    user_id = resolved.get("user_id", "")
    if not session_id or not dataset:
        config = load_config()
        session_id = session_id or get_session_id(config)
        dataset = dataset or get_dataset(config)
    return session_id, dataset, user_id


async def _store(prompt: str):
    import cognee
    from cognee.memory import QAEntry

    session_id, dataset, user_id = _load_session()
    if not session_id:
        hook_log("no_session_id", {"event": "prompt"})
        return

    config = load_config()
    await ensure_cognee_ready(config)
    user = await resolve_user(user_id)

    # Question-only QAEntry: the answer fills in on the Stop hook as
    # a separate entry. Keeping the prompt in the `question` field
    # lets recall's tokenizer search it naturally.
    entry = QAEntry(question=prompt[:MAX_TEXT], answer="", context="")

    try:
        result = await cognee.remember(
            entry,
            dataset_name=dataset,
            session_id=session_id,
            user=user,
        )
    except Exception as exc:
        hook_log("prompt_store_error", {"error": str(exc)[:200]})
        notify(f"prompt store failed ({exc})")
        return

    if result:
        hook_log(
            "prompt_stored", {"chars": len(prompt), "qa_id": getattr(result, "entry_id", None)}
        )
        notify(f"user prompt stored ({len(prompt)} chars)")
        touch_activity()


def main():
    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        hook_log("invalid_payload_json", {"event": "prompt"})
        return

    prompt = payload.get("prompt", "")
    if not prompt or len(prompt) < 5:
        return

    try:
        asyncio.run(_store(prompt))
    except Exception as exc:
        hook_log("prompt_run_exception", {"error": str(exc)[:200]})


if __name__ == "__main__":
    main()
