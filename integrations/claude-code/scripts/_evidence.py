"""Versioned evidence rendering and body-free injection receipts (stdlib only)."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from _injection_ledger import entry_hash, session_dir


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


def citation_for(reference) -> str:
    """The ``cognee://`` citation for a reference; the inverse of ``parse_citation``.

    Receipts keep only the reference (SDK-1098); this rebuilds the string form.
    """
    if not isinstance(reference, dict):
        return ""
    parts = [reference.get(k) for k in ("dataset_id", "kind", "artifact_id", "version")]
    if not all(isinstance(p, str) and p for p in parts):
        return ""
    dataset, kind, artifact, version = parts
    uri = f"cognee://datasets/{dataset}/evidence/{kind}/{artifact}@{version}"
    edge = {
        k: reference[k]
        for k in ("source_node_id", "target_node_id", "relationship_name")
        if reference.get(k)
    }
    return f"{uri}?{urlencode(edge)}" if edge else uri


#: One receipt per prompt, appended to the session's history (SDK-1098).
RECEIPTS_FILE = "recall-receipts.jsonl"
#: Entries listed per receipt; the rest are counted in ``omitted``.
MAX_RECEIPT_ITEMS = 200


def receipts_path(session_id: str) -> Path:
    return session_dir(session_id) / RECEIPTS_FILE


def prompt_hash(prompt: str) -> str:
    """Identifies a prompt across receipts without storing it."""
    return hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()[:16]


def server_origin(url: str) -> str:
    """Scheme, host, port and path only: never credentials, a query or a fragment."""
    try:
        parsed = urlparse(str(url or "").strip())
        host = parsed.hostname or ""
        if not parsed.scheme or not host:
            return ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path.rstrip('/')}"
    except ValueError:
        return ""


def receipt_scopes(per_scope: dict, skipped: str = "") -> dict:
    """``{scope: {status, retrieved, elapsed_ms}}`` from the hook's per-scope record.

    A scope that never ran takes the skip reason (``skipped_outage``,
    ``skipped_breaker``, ``skipped_budget``) when one is given.
    """
    scopes = {}
    for scope, record in (per_scope or {}).items():
        record = record if isinstance(record, dict) else {}
        status = record.get("status") or "ok"
        if record.get("skipped"):
            status = skipped or "skipped"
        scopes[scope] = {
            "status": status,
            "retrieved": int(record.get("hits", 0) or 0),
            "elapsed_ms": record.get("elapsed_ms", 0),
        }
    return scopes


def receipt_items(sections: list, stats: dict, unavailable=()) -> list:
    """One item per candidate with the decision the pipeline recorded for it."""
    kept = Counter(stats.get("entries", []))
    duplicate = Counter(stats.get("deduped_entries", []))
    dropped = Counter(stats.get("dropped_entries", []))
    items = []
    for section in sections:
        if not section.get("counted", True):
            continue
        for text in section.get("entries", []):
            if not str(text).strip():
                continue
            meta = section.get("evidence", {}).get(text) or {}
            if kept[text]:
                reason = "injected"
                kept[text] -= 1
            elif duplicate[text]:
                reason = "already_in_context"
                duplicate[text] -= 1
            elif dropped[text]:
                reason = "over_budget"
                dropped[text] -= 1
            else:
                # Neither kept, deduplicated nor dropped by the budget: a
                # pipeline gap, shown instead of being passed off as budget.
                reason = "unaccounted"
            items.append(
                {
                    "hash": entry_hash(text),
                    "reference": meta.get("reference"),
                    "source": section.get("source"),
                    "section": section.get("kind"),
                    "chars": len(text),
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
    return items


def append_receipt(
    session_id: str,
    *,
    cognee_session_id: str = "",
    prompt: str = "",
    server_url: str = "",
    per_scope: dict | None = None,
    sections: list = (),
    stats: dict | None = None,
    unavailable=(),
    skipped: str = "",
) -> str:
    """Append this prompt's receipt and return its id.

    Never persists source text or the user's prompt: entries are hashes and
    references, the prompt a hash. The file is created 0600.
    """
    items = receipt_items(list(sections), stats or {}, unavailable)
    receipt_id = uuid.uuid4().hex
    receipt = {
        "version": 2,
        "receipt_id": receipt_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "session_id": cognee_session_id,
        "prompt_hash": prompt_hash(prompt),
        "server_url": server_origin(server_url),
        "per_scope": receipt_scopes(per_scope or {}, skipped),
        "selection": "retrieval rank, source priority, whole-item budget, session dedup",
        "items": items[:MAX_RECEIPT_ITEMS],
        "omitted": max(0, len(items) - MAX_RECEIPT_ITEMS),
    }
    if skipped:
        receipt["skipped"] = skipped
    path = receipts_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(receipt, separators=(",", ":")) + "\n").encode("utf-8")
    # One write on an O_APPEND descriptor: concurrent appends cannot interleave.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return receipt_id


def read_receipts(session_id: str) -> list:
    """Every readable receipt of the session, oldest first; bad lines are skipped."""
    try:
        lines = receipts_path(session_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    receipts = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            receipts.append(value)
    return receipts


def last_receipt(session_id: str) -> dict:
    receipts = read_receipts(session_id)
    return receipts[-1] if receipts else {}


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
