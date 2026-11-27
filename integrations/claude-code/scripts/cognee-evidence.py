#!/usr/bin/env python3
"""Explain the last automatic injection, without bodies.

``explain`` prints the newest recall receipt of the session: each recalled entry's
hash, why it was injected or not, and, for a cited recall (SDK-1099), the record
tag it carried as ``citation``. Expand a tag with ``cognee-search.sh --expand``.
"""

from __future__ import annotations

import argparse
import json
import sys

from _deadlines import bounded_call
from _evidence import last_receipt
from _plugin_common import get_session_key, resolve_host_key_outside_hook, set_session_key


def run(args):
    key = args.session_key or get_session_key()
    if not key:
        key, _ = resolve_host_key_outside_hook()
    if key:
        set_session_key(key)
    if not key:
        raise ValueError("No active host session; pass --session-key")
    receipt = last_receipt(key)
    if not receipt:
        return {"status": "unavailable", "message": "No injection receipt for this session yet"}
    # Receipts keep the reference; print the tag it stands for.
    for item in receipt.get("items", []):
        if isinstance(item, dict) and isinstance(item.get("reference"), dict):
            item["citation"] = item["reference"].get("tag") or None
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    explain = commands.add_parser(
        "explain", help="Explain last automatic injection, without bodies"
    )
    explain.add_argument("--session-key")
    args = parser.parse_args()
    try:
        result = bounded_call(lambda: run(args), 45.0)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TimeoutError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
