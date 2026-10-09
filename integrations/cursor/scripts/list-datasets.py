#!/usr/bin/env python3
"""List every Cognee dataset this launch can search (``cognee-search``'s picker).

Recall is scoped to the launch's ACTIVE dataset. When it finds nothing there,
the skills offer the user the other datasets they can read and run a graph-only
search on the chosen one — without switching. This is the listing behind that
picker: every readable dataset as ``GET /api/v1/datasets`` returns it (that is
already the caller's read set, read-only ones included — a search needs no
write access), the active one marked.

Usage:
    list-datasets.py [--others] [--session-key <host id>]

``--others`` drops the active dataset from ``datasets``. Runs under the host's
shell tool (no hook payload), so it finds the launch record the way
``switch-dataset.py`` does; without a record it still lists, with ``current``
empty. Always prints JSON (the model is the only caller). Exit 0 on a
listing, 1 when the server could not be asked — then ``{"error", "code": 1}``.

Output::

    {"current": {"name", "id", "ids": [uuid, ...]},
     "datasets": [{"name", "id", "owner_id", "current"}],
     "search": "<scripts dir>/cognee-search.sh \\"<query>\\" 10 --graph --dataset-id <id>"}
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _plugin_common import (  # noqa: E402
    cross_dataset_search_command,
    hook_log,
    list_readable_datasets,
    other_readable_datasets,
    resolve_active_dataset,
    shell_runtime_overrides,
)


def active_dataset(explicit_key: str = "") -> dict:
    """The launch's active dataset: ``{"name", "id", "ids"}`` — the same
    resolution the shell wrappers use, with the launch-wide fallback for the
    name when the record does not carry one."""
    rt = shell_runtime_overrides(host_key=explicit_key)
    name = rt["dataset"] or (resolve_active_dataset(rt["host_key"]) if rt["host_key"] else "")
    ids = [x for x in rt["dataset_ids"].split(",") if x]
    return {"name": name, "id": rt["dataset_id"], "ids": ids}


def build_listing(current: dict, rows: list, *, others_only: bool = False) -> dict:
    """Mark the active dataset in ``rows`` (a ``list_readable_datasets`` result)."""
    others = other_readable_datasets(rows, current.get("name", ""), current.get("ids") or [])
    other_ids = {row["id"] for row in others}
    marked = [{**row, "current": row["id"] not in other_ids} for row in rows]
    return {
        "current": {k: current.get(k, "") for k in ("name", "id", "ids")},
        "datasets": others if others_only else marked,
        "search": cross_dataset_search_command(),
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    others_only = "--others" in args
    explicit_key = ""
    if "--session-key" in args:
        i = args.index("--session-key")
        explicit_key = args[i + 1] if i + 1 < len(args) else ""
    try:
        current = active_dataset(explicit_key)
        rows = list_readable_datasets()
    except urllib.error.HTTPError as exc:
        error = f"GET /api/v1/datasets failed (HTTP {exc.code})"
    except Exception as exc:
        error = f"GET /api/v1/datasets failed ({exc})"
    else:
        print(json.dumps(build_listing(current, rows, others_only=others_only)))
        return 0
    hook_log("list_datasets_failed", {"error": error[:300]})
    print(json.dumps({"error": error, "code": 1}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
