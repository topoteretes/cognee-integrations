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
    path: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def document_path(document: str) -> Optional[str]:
    """The page path an ingested document name came from, if it looks like one.

    Ingesting a docs tree flattens each page's path into its name, separator
    doubled — ``setup-configuration/llm-providers`` arrives as
    ``setup-configuration__llm-providers`` — so the path is recoverable by
    reversing that.

    Returns ``None`` when the name is not shaped like a page path rather than
    guessing: a citation that links somewhere wrong is worse than one that does
    not link at all, because it still reads as a cited source.
    """
    if not document:
        return None
    name = document.strip().strip("/")
    # A scheme, whitespace, or traversal means this is not a page path.
    if not name or "://" in name or any(c.isspace() for c in name) or ".." in name:
        return None
    path = name.replace("__", "/")
    if path.endswith((".mdx", ".md")):
        path = path.rsplit(".", 1)[0]
    return path


def document_url(document: str, base_url: Optional[str]) -> Optional[str]:
    """Absolute link to a page, for a backend told where the docs are published.

    Normally left unset: the widget resolves ``path`` against the site it is
    embedded on, so one backend serves a local preview and a deployed site
    without reconfiguration. Set a base only when the docs live somewhere other
    than the page carrying the widget.
    """
    path = document_path(document) if base_url else None
    return base_url.rstrip("/") + "/" + path if path else None


def document_title(document: str) -> str:
    """A readable label: the page's own name, not its whole path."""
    leaf = (document or "").replace("__", "/").rstrip("/").rsplit("/", 1)[-1]
    return leaf.replace("-", " ") if leaf else (document or "")


def _provenance(text: Optional[str]) -> dict:
    """Parse 'data_id: d1, chunk_id: c1' into {'data_id': 'd1', ...}."""
    ids: dict = {}
    for part in (text or "").split(","):
        key, _, value = part.partition(":")
        if key.strip() in ("data_id", "chunk_id") and value.strip():
            ids[key.strip()] = value.strip()
    return ids


def split_evidence(answer: str, docs_base_url: Optional[str] = None) -> Tuple[str, List[Citation]]:
    """Split an answer into (clean prose, citations parsed from its Evidence)."""
    if not isinstance(answer, str) or _EVIDENCE_MARKER not in answer:
        return (answer or "").strip(), []

    prose, _, block = answer.partition(_EVIDENCE_MARKER)
    citations: List[Citation] = []
    seen: set = set()
    for line in block.splitlines():
        match = _BULLET.match(line.strip())
        if not match:
            continue
        ids = _provenance(match.group("provenance"))
        document = match.group("document").strip()
        # Several chunks of one page back the same answer, and to a reader they
        # are one source. Collapse on (document, snippet) so identical entries
        # do not spend the widget's few citation slots twice, while two chunks
        # that actually quote different text both survive.
        key = (document, match.group("snippet") or "")
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            Citation(
                document=document,
                snippet=(match.group("snippet") or "").strip(),
                data_id=ids.get("data_id"),
                chunk_id=ids.get("chunk_id"),
                path=document_path(document),
                url=document_url(document, docs_base_url),
                title=document_title(document),
            )
        )
    return prose.strip(), citations
