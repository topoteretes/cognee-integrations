"""Bound hook context by dropping whole entries in priority order (stdlib only)."""

from __future__ import annotations

import json
import os


def injection_limit() -> int:
    """Configuration can lower the limit, but cannot exceed the host's safe ceiling."""
    try:
        return max(1024, min(9000, int(os.environ.get("COGNEE_INJECT_MAX_CHARS", "9000"))))
    except ValueError:
        return 9000


def fit_sections(sections: list[dict], budget: int, measure=None) -> tuple[str, dict]:
    """Render sections, evicting the lowest priority last entry until they fit.

    A section has ``title``, ``entries`` (whole strings), ``priority`` (higher
    wins), and optionally ``required``. The header is a required section. Ties
    evict later entries first, preserving retrieval order. Empty section titles
    disappear with their last entry. Inputs are never mutated. A required header
    larger than the budget is a caller error; silently truncating it is unsafe.
    ``measure`` can account for the complete serialized hook envelope and notices.
    """
    kept = [[str(e) for e in s.get("entries", []) if str(e).strip()] for s in sections]
    total = sum(
        len(values)
        for section, values in zip(sections, kept)
        if not section.get("required") and section.get("counted", True)
    )

    def render(values=None):
        blocks = []
        for section, entries in zip(sections, kept if values is None else values):
            if entries:
                title = section.get("title", "")
                blocks.append(
                    (title + "\n" if title else "") + section.get("separator", "\n\n").join(entries)
                )
        return "\n\n".join(blocks)

    def encoded(value):
        return len(json.dumps(value)) - 2

    size = measure or encoded
    # Hook envelopes serialize context once; any repeated visibility header is
    # required and stays constant. Measure that fixed overhead once, then use
    # additive JSON-escaped costs for entries, headings and separators.
    required = [
        values if section.get("required") else [] for section, values in zip(sections, kept)
    ]
    required_text = render(required)
    base_size = size(required_text)
    if base_size > budget:
        raise ValueError("required injection header exceeds budget")
    overhead = base_size - encoded(required_text)
    costs = [[encoded(entry) for entry in entries] for entries in kept]
    headings = [encoded(s["title"]) + 2 if s.get("title") else 0 for s in sections]
    separators = [encoded(s.get("separator", "\n\n")) for s in sections]
    base_separator = 4 if any(required) else 0

    # Exclude indivisible entries that could not fit alone, before they can
    # evict smaller results. Fenced code remains whole even at a tiny budget.
    for i, section in enumerate(sections):
        if not section.get("required"):
            eligible = [
                (entry, cost)
                for entry, cost in zip(kept[i], costs[i])
                if base_size + base_separator + headings[i] + cost <= budget
            ]
            kept[i] = [entry for entry, _ in eligible]
            costs[i] = [cost for _, cost in eligible]

    active = sum(bool(entries) for entries in kept)
    estimated = (
        overhead
        + 4 * max(0, active - 1)
        + sum(
            headings[i] + sum(costs[i]) + separators[i] * (len(entries) - 1)
            for i, entries in enumerate(kept)
            if entries
        )
    )
    eviction_order = sorted(
        (i for i, section in enumerate(sections) if not section.get("required")),
        key=lambda i: (sections[i].get("priority", 0), -i),
    )

    def evict_until(limit):
        nonlocal estimated, active
        for i in eviction_order:
            while kept[i] and estimated > limit:
                kept[i].pop()
                estimated -= costs[i].pop()
                if kept[i]:
                    estimated -= separators[i]
                else:
                    estimated -= headings[i] + (4 if active > 1 else 0)
                    active -= 1
            if estimated <= limit:
                break

    evict_until(budget)
    text = render()
    actual = size(text)
    # Defensive fallback for a caller with a non-additive custom envelope.
    # Normal hook JSON takes exactly two full-envelope measurements total.
    while actual > budget:
        if not any(kept[i] for i in eviction_order):
            raise ValueError("required injection header exceeds budget")
        evict_until(estimated - (actual - budget))
        text = render()
        actual = size(text)
    entries = [
        entry
        for section, values in zip(sections, kept)
        if not section.get("required")
        for entry in values
    ]
    source_ids = {}
    for section, values in zip(sections, kept):
        source = section.get("source")
        if source:
            source_ids.setdefault(source, set()).update(
                section["entry_ids"][entry] for entry in values
            )
    entries_kept = sum(
        len(values)
        for section, values in zip(sections, kept)
        if not section.get("required") and section.get("counted", True)
    )
    return text, {
        "context_chars": len(text),
        "entries_kept": entries_kept,
        "entries_dropped": total - entries_kept,
        "entries": entries,
        "source_hits": {source: len(ids) for source, ids in source_ids.items()},
    }
