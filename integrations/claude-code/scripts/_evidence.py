"""Body-free injection receipts (stdlib only).

One receipt per prompt records which recalled entries were injected, already in
context or over budget (SDK-1098). An entry from a cited recall (SDK-1099) opens
with its record's tag, ``[mem:<kind>/<dataset_id>/<record_id>@<version>]``, so
its receipt item names that record, keyed ``record_id@version``. A tag without a
dataset (a server with access control off) gives ``dataset_id`` None.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from _injection_ledger import entry_hash, session_dir
from _recall_text import MEMORY_TAG_RE


def entry_reference(entry: str, tags) -> dict | None:
    """The cited record an injected entry opens with, or None.

    An entry opens with the tag, or with its ``[dataset …]`` mark and then the
    tag. Only ``tags`` (the ones the server listed) count: a captured answer
    can carry tag-shaped lines of its own.
    """
    for line in str(entry).lstrip().split("\n", 2)[:2]:
        if line.strip() in tags:
            match = MEMORY_TAG_RE.fullmatch(line.strip())
            return {
                "dataset_id": match["dataset_id"],
                "kind": match["kind"],
                "record_id": match["record_id"],
                "version": match["version"],
                "tag": match.group(0),
            }
    return None


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


def receipt_items(sections: list, stats: dict) -> list:
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
            reference = entry_reference(text, set(section.get("cited_tags", ())))
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
                    "reference": reference,
                    "source": section.get("source"),
                    "section": section.get("kind"),
                    "chars": len(text),
                    # Kept for receipt readers; the cited record's own rank is in
                    # the recall response, not in the injected text.
                    "rank": None,
                    "priority": section.get("priority", 0),
                    "reason": reason,
                    "addressable": reference is not None,
                }
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
    skipped: str = "",
) -> str:
    """Append this prompt's receipt and return its id.

    Never persists source text or the user's prompt: entries are hashes and
    references, the prompt a hash. The file is created 0600.
    """
    items = receipt_items(list(sections), stats or {})
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
