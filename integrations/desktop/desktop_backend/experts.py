"""Who knows about X — expert finding from memory provenance.

Every graph chunk carries ``source_user`` (who ingested it) and handover
notes carry a ``from:`` line (who shared the learning). Aggregating both over
the chunks that match a query ranks the people whose memory footprint covers
the topic. On a single-key demo tenant most ``source_user`` values collapse
to one account; handover senders still differentiate.
"""

from __future__ import annotations

import re
from typing import Any

_FROM_LINE = re.compile(r"(?m)^- from:\s*(\S+)")


async def find_experts(adapter: Any, query: str, limit: int = 5) -> list[dict[str, Any]]:
    if not hasattr(adapter, "_search_raw"):
        return []
    raw = await adapter._search_raw(query, "CHUNKS", 20, scope_all=True)
    counts: dict[str, int] = {}
    for envelope in raw or []:
        if not isinstance(envelope, dict):
            continue
        for chunk in envelope.get("search_result") or []:
            if not isinstance(chunk, dict):
                continue
            names: set[str] = set()
            if user := str(chunk.get("source_user") or ""):
                names.add(user.split("@")[0])
            for sender in _FROM_LINE.findall(str(chunk.get("text", ""))):
                names.add(sender)
            for name in names:
                counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"name": name, "evidence": count} for name, count in ranked]
