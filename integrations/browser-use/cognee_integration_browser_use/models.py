"""Record and receipt contracts.

A :class:`MemoryRecord` is what one run leaves behind: what was attempted, what
was observed, where (sanitized source URLs), and how much to trust it. That
provenance is written into the document text itself so it survives any backend
version's metadata handling. The record's identity (``record_id``, ``run_id``)
and its observation time are deliberately *not* in the text: identical content
must serialize to identical bytes so the content-derived ``upload_name`` names
exactly one document and the server can deduplicate it. They live in the
receipt ledger instead.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class RecordKind(str, Enum):
    RESEARCH_FINDING = "research_finding"
    PROCEDURE = "procedure"
    FAILED_ATTEMPT = "failed_attempt"
    PREFERENCE = "preference"
    INTERRUPTED_RUN = "interrupted_run"
    OBSERVATION = "observation"  # model-proposed via cognee_remember


class Verification(str, Enum):
    UNVERIFIED = "unverified"
    AGENT_REPORTED = "agent_reported"
    APPLICATION_VALIDATED = "application_validated"


class ReceiptStatus(str, Enum):
    ACCEPTED = "accepted"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    UNKNOWN = "unknown"
    DELETED = "deleted"


PENDING_STATUSES = (ReceiptStatus.ACCEPTED, ReceiptStatus.INDEXING, ReceiptStatus.UNKNOWN)


def new_record_id() -> str:
    return "rec_" + uuid.uuid4().hex[:24]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class MemoryRecord:
    scope_key: str
    run_id: str
    kind: RecordKind
    task: str
    summary: str
    outcome: str
    source_urls: list[str] = field(default_factory=list)
    observed_at: datetime = field(default_factory=utcnow)
    verification: Verification = Verification.UNVERIFIED
    record_id: str = field(default_factory=new_record_id)
    steps: Optional[int] = None

    def to_text(self) -> str:
        kind_label = self.kind.value.replace("_", " ")
        lines = [
            f"Browser research record ({kind_label})",
            f"Verification: {self.verification.value}",
            f"Outcome: {self.outcome}" + (f" after {self.steps} steps" if self.steps else ""),
            "",
            "Task:",
            self.task,
            "",
            "Observed result:",
            self.summary,
        ]
        if self.source_urls:
            lines += ["", "Sources (as observed at the time):"]
            lines += [f"- {url}" for url in self.source_urls]
        lines += [
            "",
            "Note: this is historical evidence from one browser run; facts may have changed.",
        ]
        return "\n".join(lines) + "\n"

    @property
    def content_hash(self) -> str:
        """Hash of what the record *says*, which is also all that ``to_text`` writes.

        ``record_id``, ``run_id`` and ``observed_at`` are left out of both: two
        runs that observe the same thing produce one hash, one upload name and
        byte-identical documents, so the ledger's dedupe (``find_live_by_hash``)
        and the server's own content dedupe both stop a second cognify pass.
        """
        parts = [
            self.scope_key,
            self.kind.value,
            self.verification.value,
            self.task,
            self.summary,
            self.outcome,
            *sorted(self.source_urls),
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    @property
    def upload_name(self) -> str:
        return f"browser-{self.kind.value}-{self.content_hash[:32]}"


@dataclass
class Receipt:
    record_id: str
    run_id: str
    scope_key: str
    dataset: str
    upload_name: str
    content_hash: str
    kind: str
    status: ReceiptStatus
    created_at: str
    updated_at: str
    dataset_id: Optional[str] = None
    data_id: Optional[str] = None
    error: Optional[str] = None
    durable: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["status"] = self.status.value
        return data


@dataclass
class RecallHit:
    text: str
    dataset: str
    score: Optional[float] = None
    document_name: Optional[str] = None
    data_id: Optional[str] = None


@dataclass
class RecallContext:
    status: str  # "ok" | "empty" | "disabled" | "unavailable" | "error"
    query: str
    datasets: list[str]
    hits: list[RecallHit] = field(default_factory=list)
    truncated: bool = False
    error: Optional[str] = None

    @property
    def text(self) -> str:
        """The bounded evidence block, empty when nothing was retrieved."""
        if not self.hits:
            return ""
        parts = []
        for index, hit in enumerate(self.hits, 1):
            provenance = [p for p in (hit.document_name and f"record {hit.document_name}",) if p]
            if hit.score is not None:
                provenance.append(f"score {hit.score:.2f}")
            label = ", ".join(provenance) or "stored record"
            parts.append(f"{index}. [{label}]\n{hit.text}")
        return "\n\n".join(parts)


@dataclass
class MemoryRunStatus:
    """Memory outcomes for one run, kept apart from the browser result."""

    run_id: str
    dataset: str
    recall: str = "not_run"
    recall_error: Optional[str] = None
    capture: str = "not_run"
    capture_error: Optional[str] = None
    receipts: list[Receipt] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    steps_observed: int = 0
    durable: bool = True

    @property
    def ok(self) -> bool:
        return self.recall_error is None and self.capture_error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset": self.dataset,
            "recall": self.recall,
            "recall_error": self.recall_error,
            "capture": self.capture,
            "capture_error": self.capture_error,
            "receipts": [r.to_dict() for r in self.receipts],
            "tool_calls": list(self.tool_calls),
            "steps_observed": self.steps_observed,
            "durable": self.durable,
        }
