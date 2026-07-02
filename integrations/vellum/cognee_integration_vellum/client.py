"""The single cognee-facing surface for the Vellum integration.

Everything the Workflow nodes and the Agent Node tools do goes through here, and
it is built only on cognee's public ``remember()`` / ``recall()`` API — no
reimplemented ingestion or session handling.
"""

import asyncio
import concurrent.futures
from typing import Any, Optional

import cognee

from . import bootstrap  # noqa: F401  (loads .env on import)

DEFAULT_DATASET_NAME = "main_dataset"


def run_sync(coro):
    """Run an async cognee call from Vellum's synchronous ``node.run()`` / tools.

    Uses ``asyncio.run()`` when no event loop is active. If a loop is already
    running (the caller is itself async), the coroutine is run in a worker
    thread so we never touch the running loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


async def remember(
    data,
    *,
    dataset_name: str = DEFAULT_DATASET_NAME,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    run_in_background: bool = False,
):
    """Store data in cognee memory via the public ``remember()`` API.

    ``run_in_background`` defaults to ``False`` (sync): ``remember()`` blocks
    until the pipeline finishes, so the caller gets a real terminal status.
    Per-end-user scoping maps ``user_id`` onto a cognee node set; one Vellum
    workflow deployment maps to one ``dataset_name`` by default.
    """
    kwargs: dict[str, Any] = {
        "dataset_name": dataset_name,
        "run_in_background": run_in_background,
    }
    if user_id:
        kwargs["node_set"] = [user_id]
    if session_id:
        kwargs["session_id"] = session_id

    return await cognee.remember(data, **kwargs)


async def recall(
    query_text: str,
    *,
    dataset_name: Optional[str] = DEFAULT_DATASET_NAME,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    top_k: int = 15,
    include_references: bool = True,
):
    """Retrieve from cognee memory via the public ``recall()`` API.

    ``include_references`` is on by default so recall results carry the source
    lineage (which dataset/document/chunk each hit came from) that the nodes
    expose as typed ``citations``.
    """
    kwargs: dict[str, Any] = {
        "top_k": top_k,
        "include_references": include_references,
    }
    if dataset_name:
        kwargs["datasets"] = [dataset_name]
    if user_id:
        kwargs["node_name"] = [user_id]
    if session_id:
        kwargs["session_id"] = session_id

    return await cognee.recall(query_text, **kwargs)


def extract_answer_and_citations(responses):
    """Flatten cognee ``recall()`` responses into a renderable answer plus typed
    citations (which dataset/document/chunk each hit came from).

    ``recall()`` returns a discriminated union of entry types, and the answer
    text lives in a different field per type: ``text`` on graph hits
    (SearchResultItem), ``content`` on graph/session context entries, and
    ``answer`` on session QA entries. We read all three so a session-based
    recall (the support-assistant case) isn't dropped. ``dataset_name`` /
    ``dataset_id`` / ``metadata`` / ``qa_id`` carry the source lineage.
    """
    answer_parts = []
    citations = []

    for r in responses:
        text = getattr(r, "content", None) or getattr(r, "text", None) or getattr(r, "answer", None)
        if text:
            answer_parts.append(text)

        citation: dict[str, Any] = {"source": getattr(r, "source", None)}
        for field in ("dataset_name", "dataset_id", "score", "qa_id"):
            value = getattr(r, field, None)
            if value is not None:
                citation[field] = value
        metadata = getattr(r, "metadata", None)
        if metadata:
            # chunk_id / doc_id live here
            citation["metadata"] = metadata
        citations.append(citation)

    return "\n\n".join(answer_parts), citations
