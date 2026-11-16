#!/usr/bin/env python3
"""Inspect the recall index, expand citations in one batch, or explain injection."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error

from _deadlines import bounded_call
from _evidence import parse_citation
from _injection_ledger import session_dir
from _plugin_common import (
    _json_http_request,
    get_session_key,
    recall_via_http,
    resolve_active_dataset_ids,
    resolve_host_key_outside_hook,
    set_session_key,
)


def run(args):
    if args.command == "details":
        refs = (
            json.loads(args.references)
            if args.references
            else [parse_citation(c) for c in args.citations]
        )
        if not isinstance(refs, list) or not 1 <= len(refs) <= 5:
            raise ValueError("Expand one to five references per batch")
        return _json_http_request("/api/v1/recall/evidence", {"references": refs}, timeout=45.0)
    key = args.session_key or get_session_key()
    if not key:
        key, _ = resolve_host_key_outside_hook()
    if key:
        set_session_key(key)
    if args.command == "explain":
        if not key:
            raise ValueError("No active host session; pass --session-key")
        path = session_dir(key) / "injection-receipt.json"
        if not path.exists():
            return {"status": "unavailable", "message": "No injection receipt for this session yet"}
        return json.loads(path.read_text(encoding="utf-8"))
    dataset, ids = resolve_active_dataset_ids()
    if args.dataset:
        dataset, ids = args.dataset, []
    from _capture_policy import sanitize

    result = recall_via_http(
        sanitize(args.query),
        session_id="",
        top_k=args.top_k,
        scope=["graph"],
        dataset=dataset,
        dataset_ids=ids,
        evidence_mode="index",
        timeout=45.0,
    )
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return {
            "status": "unsupported",
            "items": [],
            "message": "Evidence index unsupported. Use cognee-search.sh for legacy search.",
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="Compact body-free search index")
    index.add_argument("query")
    index.add_argument("--dataset", "--dataset-id", dest="dataset")
    index.add_argument("--top-k", type=int, choices=range(1, 21), default=5)
    index.add_argument("--session-key")
    details = commands.add_parser("details", help="Expand up to five citations in one HTTP request")
    details.add_argument("citations", nargs="*")
    details.add_argument("--references", help="JSON array of reference objects from the index")
    explain = commands.add_parser(
        "explain", help="Explain last automatic injection, without bodies"
    )
    explain.add_argument("--session-key")
    args = parser.parse_args()
    try:
        result = bounded_call(lambda: run(args), 45.0)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except urllib.error.HTTPError as exc:
        status = "unsupported" if exc.code in {404, 405} else "error"
        print(json.dumps({"status": status, "http_status": exc.code}), file=sys.stderr)
    except (ValueError, OSError, TimeoutError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
