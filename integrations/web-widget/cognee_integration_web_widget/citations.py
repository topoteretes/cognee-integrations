"""Turn cognee's Evidence block into inline citations.

``recall(..., include_references=True)`` grounds an answer by appending
an ``Evidence:`` block to the answer text — one bullet per source chunk::

    <answer prose>

    Evidence:
    - chunk 3 of document report.pdf (data_id: d1, chunk_id: c1): "…snippet…"

The quoted snippet is optional, and Cognee Cloud omits it — its bullets stop at
the closing parenthesis::

    - chunk 1 of document cognee-cloud__api-keys (data_id: 981bfccc-…, chunk_id: 7f5ee6e7-…)

so a citation may carry a document and ids but no quoted text.

The widget shows the clean prose and renders each bullet as a citation below
it. This module does the split. Answers with no Evidence block (for example a
plain session-memory recall) simply carry no citations — we never fabricate a
source by re-quoting the answer.
"""

from __future__ import annotations

import dataclasses
import re
from typing import List, Optional, Tuple

# The exact separator cognee inserts before the block (EVIDENCE_HEADER in
# cognee/modules/retrieval/utils/references.py, appended as "\n\n" + header).
_EVIDENCE_MARKER = "\n\nEvidence:\n"

# - chunk 3 of document report.pdf (data_id: d1, chunk_id: c1): "snippet"
# Both trailing parts are optional: the parenthetical when no ids are known, and
# the quoted snippet, which Cognee Cloud never emits. Requiring the snippet made
# every Cloud bullet fail to match, so answers silently arrived with no sources
# at all — the widget's one grounding feature, absent without an error.
_BULLET = re.compile(
    r"-\s*chunk\s+\d+\s+of\s+document\s+(?P<document>.+?)"
    r"(?:\s+\((?P<provenance>[^)]*)\))?"
    r'(?::\s*"(?P<snippet>.*)")?\s*$'
)


@dataclasses.dataclass
class Citation:
    """One source chunk backing an answer."""

    document: str
    snippet: str
    data_id: Optional[str] = None
    chunk_id: Optional[str] = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _provenance(text: Optional[str]) -> dict:
    """Parse 'data_id: d1, chunk_id: c1' into {'data_id': 'd1', ...}."""
    ids: dict = {}
    for part in (text or "").split(","):
        key, _, value = part.partition(":")
        if key.strip() in ("data_id", "chunk_id") and value.strip():
            ids[key.strip()] = value.strip()
    return ids


def split_evidence(answer: str) -> Tuple[str, List[Citation]]:
    """Split an answer into (clean prose, citations parsed from its Evidence)."""
    if not isinstance(answer, str) or _EVIDENCE_MARKER not in answer:
        return (answer or "").strip(), []

    prose, _, block = answer.partition(_EVIDENCE_MARKER)
    citations: List[Citation] = []
    for line in block.splitlines():
        match = _BULLET.match(line.strip())
        if not match:
            continue
        ids = _provenance(match.group("provenance"))
        citations.append(
            Citation(
                document=match.group("document").strip(),
                snippet=(match.group("snippet") or "").strip(),
                data_id=ids.get("data_id"),
                chunk_id=ids.get("chunk_id"),
            )
        )
    return prose.strip(), citations
