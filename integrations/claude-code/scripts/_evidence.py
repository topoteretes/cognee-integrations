"""Versioned evidence rendering and body-free injection receipts (stdlib only)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from _injection_ledger import atomic_json, entry_hash, session_dir


def parse_citation(value: str) -> dict:
    uri = urlparse(value)
    parts = uri.path.strip("/").split("/")
    if uri.scheme != "cognee" or uri.netloc != "datasets" or len(parts) != 4:
        raise ValueError("Expected a cognee evidence citation")
    dataset, marker, kind, identity = parts
    if marker != "evidence" or "@" not in identity:
        raise ValueError("Citation must include its version")
    artifact, version = identity.split("@", 1)
    ref = {"dataset_id": dataset, "kind": kind, "artifact_id": artifact, "version": version}
    for key, values in parse_qs(uri.query).items():
        if key in {"source_node_id", "target_node_id", "relationship_name"}:
            ref[key] = values[0]
    return ref


def evidence_items(entry: dict) -> list | None:
    meta = entry.get("metadata")
    if not isinstance(meta, dict) or meta.get("evidence_contract") != "recall_evidence_v1":
        return None
    items = meta.get("evidence_items")
    return items if isinstance(items, list) else None


def render_item(item: dict) -> str:
    if item.get("status") != "available" or not item.get("text") or not item.get("citation"):
        return ""
    source = item.get("source") or {}
    label = "Derived graph evidence" if source.get("derived") else "Source evidence"
    suffix = " [excerpt; expand for detail]" if item.get("truncated") else ""
    return (
        f"{label}: {item.get('title') or 'Evidence'}{suffix}\n{item['text']}\n[{item['citation']}]"
    )


def write_receipt(session_id: str, sections: list, stats: dict, unavailable=()) -> None:
    """Replace the last receipt; never persist source text or the user's prompt."""
    kept = Counter(stats.get("entries", []))
    duplicate = Counter(stats.get("deduped_entries", []))
    items = []
    for section in sections:
        if not section.get("counted", True):
            continue
        for text in section.get("entries", []):
            meta = section.get("evidence", {}).get(text) or {}
            if kept[text]:
                reason = "injected"
                kept[text] -= 1
            elif duplicate[text]:
                reason = "already_in_context"
                duplicate[text] -= 1
            else:
                reason = "over_budget"
            items.append(
                {
                    "hash": entry_hash(text),
                    "reference": meta.get("reference"),
                    "citation": meta.get("citation"),
                    "source": section.get("source"),
                    "rank": meta.get("rank"),
                    "priority": section.get("priority", 0),
                    "reason": reason,
                    "addressable": bool(meta.get("reference")),
                }
            )
    for item in unavailable:
        items.append(
            {"reference": item.get("reference"), "reason": item.get("status", "unavailable")}
        )
    # Bounded last-turn diagnostics, not a second memory store.
    atomic_json(
        session_dir(session_id) / "injection-receipt.json",
        {
            "version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "selection": "retrieval rank, source priority, whole-item budget, session dedup",
            "items": items[:200],
            "omitted": max(0, len(items) - 200),
        },
    )


def merge_indexes(first: dict, second) -> dict:
    """Bound a project-companion + primary index without losing scope or versions."""
    if not isinstance(second, dict) or not isinstance(second.get("items"), list):
        return {**first, "partial": True}
    items, seen = [], set()
    for item in first.get("items", []) + second["items"]:
        ref = item.get("reference") or {}
        key = tuple(ref.get(k) for k in ("dataset_id", "kind", "artifact_id", "version"))
        if key not in seen:
            items.append(item)
            seen.add(key)
    return {
        "items": items[:20],
        "omitted": first.get("omitted", 0) + second.get("omitted", 0) + max(0, len(items) - 20),
        "unaddressable_results": first.get("unaddressable_results", 0)
        + second.get("unaddressable_results", 0),
        "partial": bool(first.get("partial") or second.get("partial")),
    }
