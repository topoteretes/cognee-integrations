"""Opt-in live acceptance against a real cognee server.

    COGNEE_LIVE=1 COGNEE_BASE_URL=... COGNEE_API_KEY=... uv run pytest tests/live -v

Process 1 captures a run outcome; a *separate* Python process recalls it; the
record is deleted and recall is checked again. Uses a disposable dataset.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid

import pytest
from cognee_integration_browser_use import CogneeBrowserMemory, MemoryScope, ReceiptStatus
from tests.conftest import FakeAgent, FakeHistory

LIVE = (
    os.environ.get("COGNEE_LIVE") == "1"
    and os.environ.get("COGNEE_BASE_URL")
    and os.environ.get("COGNEE_API_KEY")
)
pytestmark = pytest.mark.skipif(
    not LIVE, reason="set COGNEE_LIVE=1, COGNEE_BASE_URL, COGNEE_API_KEY"
)

CHILD = """
import asyncio, json, sys
from cognee_integration_browser_use import CogneeBrowserMemory, MemoryScope
scope = MemoryScope(tenant_id=sys.argv[1], user_id=sys.argv[2])
async def main():
    async with CogneeBrowserMemory() as memory:
        run = memory.new_run(task=sys.argv[3], scope=scope)
        ctx = await run.recall_context()
        print(json.dumps({"status": ctx.status, "text": ctx.text, "error": ctx.error}))
asyncio.run(main())
"""


async def _wait_ready(memory: CogneeBrowserMemory, record_id: str, timeout_s: float = 300.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        receipt = await memory.record_status(record_id)
        if receipt and receipt.status in (
            ReceiptStatus.READY,
            ReceiptStatus.FAILED,
            ReceiptStatus.UNKNOWN,
        ):
            return receipt
        await asyncio.sleep(5)
    return await memory.record_status(record_id)


async def test_capture_in_one_process_recall_in_another_then_delete(tmp_path):
    tenant = "live-" + uuid.uuid4().hex[:8]
    marker = "Quokka-" + uuid.uuid4().hex[:6]
    scope = MemoryScope(tenant_id=tenant, user_id="buyer")
    other = MemoryScope(tenant_id=tenant, user_id="someone-else")
    history = FakeHistory(
        final=f"Supplier {marker} ships pallets in 3 business days; minimum order 20 units.",
        urls=["https://supplier.example/catalog?session=abc", "https://supplier.example/terms"],
    )
    env = {**os.environ, "COGNEE_BROWSER_USE_RECEIPTS_PATH": str(tmp_path / "receipts.sqlite")}

    async with CogneeBrowserMemory(receipts_path=str(tmp_path / "receipts.sqlite")) as memory:
        run = memory.new_run(task=f"research supplier {marker} lead time", scope=scope)
        await run.execute(FakeAgent(steps=2, history=history))
        assert run.status.capture == "accepted", run.status.to_dict()
        record_id = run.status.receipts[0].record_id
        receipt = await _wait_ready(memory, record_id)
        assert receipt.status == ReceiptStatus.READY, receipt.to_dict()

        def recall_in_child(user: str) -> dict:
            out = subprocess.run(
                [sys.executable, "-c", CHILD, tenant, user, f"supplier {marker} lead time"],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            assert out.returncode == 0, out.stderr[-2000:]
            return json.loads(out.stdout.strip().splitlines()[-1])

        found = None
        for _ in range(6):
            found = recall_in_child("buyer")
            if found["status"] == "ok" and marker in found["text"]:
                break
            await asyncio.sleep(5)
        assert found and marker in found["text"], found
        assert "session=abc" not in found["text"]

        isolated = recall_in_child("someone-else")
        assert marker not in (isolated["text"] or ""), isolated
        assert isolated["status"] in {"empty", "ok"}

        deleted = await memory.forget_record(record_id, scope=scope)
        assert deleted["verified"] is True, deleted
        again = await memory.new_run(
            task=f"supplier {marker} lead time", scope=scope
        ).recall_context()
        assert marker not in again.text, again
        # Another scope can never touch this record, deleted or not.
        assert await memory.forget_record(record_id, scope=other) == {
            "record_id": record_id,
            "verified": False,
            "status": "scope_mismatch",
        }
        assert (await memory.forget_record(record_id, scope=scope))["status"] == "deleted"
