"""Latent features — built, tested, and dark until ``SPOTLIGHT_EXPERIMENTS=true``.

Three capabilities that ride the existing plumbing:

- **Answer feedback**: 👍 re-ingests the confirmed Q&A into the user's own
  dataset as a "confirmed learning" (retrieval reinforcement by document —
  the tenant exposes no rating API, so this is the honest version); 👎 is
  logged to ``feedback.jsonl`` for later correction workflows.
- **Contradiction surfacing**: cognee records ``contradicts`` edges instead
  of overwriting conflicting facts; this scans dataset graphs for such edges
  touching the query's terms so the UI can show "conflicting memory".
- **Temporal detection**: queries with time cues route to cognee's TEMPORAL
  search type instead of plain graph completion.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_TEMPORAL_CUE = re.compile(
    r"(?i)\b(when|yesterday|today|last (week|month|year|quarter)|this (week|month|year)|"
    r"since|until|before|after|during|in (january|february|march|april|may|june|july|"
    r"august|september|october|november|december|20\d\d)|on \d{1,2})\b"
)


def is_temporal(query: str) -> bool:
    return bool(_TEMPORAL_CUE.search(query))


async def record_feedback(
    adapter: Any, data_dir: Path, query: str, answer: str, rating: int
) -> str:
    """Log the rating; positive ratings reinforce memory with the confirmed Q&A."""
    log = data_dir / "feedback.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "q": query, "rating": rating}) + "\n")
    if rating >= 4 and hasattr(adapter, "remember"):
        note = f"# Confirmed answer\n\nQ: {query}\n\nA (user-confirmed): {answer}\n"
        await adapter.remember(
            note, filename=f"confirmed-{int(time.time())}.md", node_set="feedback"
        )
        return "reinforced"
    return "logged"


# dataset name -> (fetched_at, contradiction edge list)
_contradiction_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


async def contradictions_for(adapter: Any, query: str, ttl: float = 600.0) -> list[dict]:
    """``contradicts`` edges whose endpoints overlap the query's terms."""
    if not hasattr(adapter, "_request"):
        return []
    terms = {w for w in re.findall(r"[a-z]{4,}", query.lower())}
    if not terms:
        return []
    listing = await adapter._request("GET", "/api/v1/datasets")
    if listing.status_code >= 400:
        return []
    hits: list[dict] = []
    for ds in listing.json() if isinstance(listing.json(), list) else []:
        name, ds_id = str(ds.get("name", "")), str(ds.get("id", ""))
        if name in getattr(adapter, "exclude_datasets", set()):
            continue
        edges = await _contradiction_edges(adapter, name, ds_id, ttl)
        for edge in edges:
            blob = f"{edge['a']} {edge['b']}".lower()
            if any(term in blob for term in terms):
                hits.append({**edge, "dataset": name})
    return hits


async def _contradiction_edges(
    adapter: Any, name: str, ds_id: str, ttl: float
) -> list[dict[str, str]]:
    stamp, cached = _contradiction_cache.get(name, (0.0, []))
    if time.time() - stamp < ttl:
        return cached
    try:
        g = await adapter._request("GET", f"/api/v1/datasets/{ds_id}/graph")
        if g.status_code >= 400:
            raise ValueError(g.status_code)
        payload = g.json()
        labels = {str(n.get("id", "")): str(n.get("label", "")) for n in payload.get("nodes", [])}
        edges = [
            {
                "a": labels.get(str(e.get("source", "")), ""),
                "b": labels.get(str(e.get("target", "")), ""),
                "relation": str(e.get("label", "")),
            }
            for e in payload.get("edges", [])
            if "contradict" in str(e.get("label", "")).lower()
        ]
    except Exception:
        edges = []
    _contradiction_cache[name] = (time.time(), edges)
    return edges


class ConversationThreads:
    """Client-side follow-up context: the tenant's search API is stateless,
    so the previous turn rides along inside the next query."""

    def __init__(self, max_threads: int = 50) -> None:
        self._threads: dict[str, tuple[str, str]] = {}
        self._max = max_threads

    def contextualize(self, thread: str, query: str) -> str:
        prior = self._threads.get(thread)
        if not prior:
            return query
        prev_q, prev_a = prior
        return (
            f"Earlier in this conversation the user asked: {prev_q!r} and the "
            f"answer was: {prev_a[:400]!r}. Follow-up question: {query}"
        )

    def remember_turn(self, thread: str, query: str, answer: str) -> None:
        if len(self._threads) >= self._max:
            self._threads.pop(next(iter(self._threads)))
        self._threads[thread] = (query, answer)
