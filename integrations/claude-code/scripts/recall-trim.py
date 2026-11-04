#!/usr/bin/env python3
"""Trim a recall result the way the prompt hook does, for the hooks module.

``cognee-search.sh`` prints what ``/api/v1/recall`` answered. On cognee >= 1.6.0
a graph item's ``text`` is the whole prompt the server's own LLM would have
received (history, question, context, guidance); the prompt hook keeps only the
retrieved context (``trim_recall_text``) and renders a code item's facts one
per line (``format_code_facts``). The module's ``search`` tool and the memory
pane's search box run the wrapper too, so they pipe its output through this
script to show the same thing the hook injects.

Usage:
    recall-trim.py --query "<the search query>" < recall.json

Reads the wrapper's stdout on stdin and prints the same JSON list, each entry's
``text`` trimmed. Anything that is not a JSON list (an error envelope, the
UNREACHABLE sentinel, plain text) is printed back unchanged, so the caller's
reading of it is unaffected. Never exits non-zero on bad input.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _recall_text import code_facts, format_code_facts, trim_recall_text  # noqa: E402


def trim_entries(entries: list, query: str) -> list:
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        source = str(entry.get("source", "") or "")
        text = entry.get("text")
        if not isinstance(text, str) or not text:
            out.append(entry)
            continue
        if source == "code":
            facts = code_facts(text)
            if facts:
                out.append({**entry, "text": format_code_facts(facts)})
                continue
            out.append(entry)
            continue
        trimmed, _guidance, _stats = trim_recall_text(text, query)
        out.append({**entry, "text": trimmed})
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    query = ""
    if "--query" in args:
        i = args.index("--query")
        query = args[i + 1] if i + 1 < len(args) else ""
    raw = sys.stdin.read()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        sys.stdout.write(raw)
        return 0
    if not isinstance(parsed, list):
        sys.stdout.write(raw)
        return 0
    try:
        print(json.dumps(trim_entries(parsed, query), ensure_ascii=False))
    except Exception:
        sys.stdout.write(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
