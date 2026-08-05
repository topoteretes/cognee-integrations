"""Result-shape helpers shared by every adapter.

cognee's search results changed shape over versions (flat lists before ~1.2,
per-dataset envelopes after) and differ between transports. These helpers give
integrations one stable, flat shape to consume regardless of the cognee
version behind them.
"""

from __future__ import annotations

from typing import Any, Optional


def unwrap_results(results: Optional[list[Any]]) -> list[Any]:
    """Flatten cognee's per-dataset envelopes.

    Since cognee ~1.2, ``search`` returns ``[{dataset_id, dataset_name,
    search_result: [...]}, ...]``; older versions return the flat list. The
    inner items are what integrations consume (chunk dicts for CHUNKS, answer
    strings for GRAPH_COMPLETION).
    """
    out: list[Any] = []
    for item in results or []:
        if isinstance(item, dict) and "search_result" in item:
            inner = item["search_result"]
            out.extend(inner if isinstance(inner, list) else [inner])
        else:
            out.append(item)
    return out


_REFUSAL = None  # compiled lazily


def completions_by_dataset(raw_results: Optional[list[Any]]) -> list[tuple[str, str]]:
    """(dataset_name, completion_text) pairs from a raw envelope list.

    Feeds answer attribution: which memory layers actually had something to
    say. Flat (non-envelope) results map to a single unnamed dataset.
    """
    pairs: list[tuple[str, str]] = []
    for item in raw_results or []:
        if isinstance(item, dict) and "search_result" in item:
            name = str(item.get("dataset_name", ""))
            inner = item["search_result"]
            text = best_text(inner if isinstance(inner, list) else [inner])
            if text:
                pairs.append((name, text))
        else:
            text = best_text([item])
            if text:
                pairs.append(("", text))
    return pairs


def _is_refusal(text: str) -> bool:
    """Whether a completion is a polite "nothing here" rather than an answer."""
    global _REFUSAL
    if _REFUSAL is None:
        import re

        _REFUSAL = re.compile(
            r"(?i)\b(sorry|does not contain|doesn'?t contain|no information|"
            r"cannot be determined|not enough information|contains only|"
            r"no relevant|unable to answer|I don'?t have)\b"
        )
    pattern = _REFUSAL
    assert pattern is not None
    return bool(pattern.search(text[:250]))


def best_text(results: Optional[list[Any]]) -> str:
    """The most informative completion in a result list.

    A multi-dataset search returns one completion per dataset, and datasets
    with irrelevant context produce polite refusals ("the graph contains only
    technical entities…"). Taking the first item hands the refusal to the
    user whenever an irrelevant dataset happens to be listed first — so:
    prefer non-refusals, and among those the most substantial answer.
    """
    texts: list[str] = []
    for item in results or []:
        if isinstance(item, str) and item.strip():
            texts.append(item.strip())
        elif isinstance(item, dict):
            for key in ("text", "content", "answer"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())
                    break
    substantial = [t for t in texts if not _is_refusal(t)]
    pool = substantial or texts
    return max(pool, key=len) if pool else ""


def first_text(results: Optional[list[Any]]) -> str:
    """The first renderable text in a completion result list, whatever the shape."""
    for item in results or []:
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, dict):
            for key in ("text", "content", "answer"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def chunk_text(chunk: dict[str, Any]) -> str:
    """The renderable text of one chunk result."""
    for key in ("text", "content", "chunk", "answer"):
        value = chunk.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_file_hint(chunk: dict[str, Any]) -> Optional[str]:
    """Best-effort file path / document name buried in a chunk result.

    Chunk payload shapes differ between cognee versions and transports, so
    this scans (depth-first) for the usual keys rather than assuming one
    schema.
    """
    stack: list[Any] = [chunk]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for key in ("file_path", "raw_data_location", "document_name", "name", "source"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for v in node.values():
            if isinstance(v, dict):
                stack.append(v)
            elif isinstance(v, list):
                stack.extend(x for x in v if isinstance(x, dict))
    return None
