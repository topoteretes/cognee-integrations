"""Exact, atomic per-host-session counters; no prompt or memory text is stored."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from _file_lock import file_lock
from _injection_ledger import atomic_json, session_dir

COUNTERS = (
    "recalls",
    "hits",
    "entries_injected",
    "entries_deduped",
    "chars_injected",
    "dedup_chars_saved",
    "dedup_measurements",
    "saves",
    "improves_landed",
    "digest_items_used",
    "traces_skipped",
    "traces_skipped_self",
    "traces_skipped_low_signal",
    "bash_post_tool_use",
    "bash_traces_captured",
    "bash_skipped_self",
    "bash_skipped_low_signal",
    "distillation_entries",
    "recall_ms",
)


def read(session_key: str) -> dict:
    try:
        value = json.loads((session_dir(session_key) / "stats.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def update(session_key: str, *, metadata: dict = None, **increments) -> bool:
    if not session_key:
        return False
    try:
        directory = session_dir(session_key)
        with file_lock(directory / "stats.lock", timeout=0.2) as acquired:
            if not acquired:
                return False
            data = read(session_key)
            data.setdefault("started_at", time.time())
            data["updated_at"] = time.time()
            data["host_session_id"] = session_key
            for key in COUNTERS:
                data[key] = data.get(key, 0) + max(0, increments.get(key, 0))
            for key, value in (metadata or {}).items():
                if key in {"dataset", "server_url", "session_id", "ended_at"}:
                    data[key] = value
            atomic_json(directory / "stats.json", data)
        return True
    except (OSError, ValueError, TypeError):
        return False


def previous(current: str, dataset: str = "", server_url: str = "") -> dict:
    candidates = []
    root = Path.home() / ".cognee-plugin" / "codex" / "sessions"
    for path in root.glob("*/stats.json"):
        if path.parent.name == current:
            continue
        data = read(path.parent.name)
        if not data.get("ended_at"):
            continue
        if dataset and data.get("dataset") != dataset:
            continue
        if server_url and data.get("server_url") != server_url:
            continue
        candidates.append(data)
    return max(candidates, key=lambda d: d.get("ended_at", 0), default={})


def summary(data: dict) -> str:
    if not data:
        return ""
    recalls, hits = int(data.get("recalls", 0)), int(data.get("hits", 0))
    rate = f"{hits}/{recalls} ({hits / recalls:.0%})" if recalls else "unmeasured"
    return (
        f"Last session: Cognee saved {int(data.get('saves', 0))} entries; "
        f"{int(data.get('improves_landed', 0))} improves landed; "
        f"{int(data.get('distillation_entries', 0))} distillation entries confirmed; "
        f"recall hit rate {rate}."
    )


def report(session_key: str = "", days: int = 7) -> dict:
    cutoff = time.time() - days * 86400
    root = Path.home() / ".cognee-plugin" / "codex" / "sessions"
    # Keep the seven-day total exact: sessions started before the window are
    # excluded rather than attributing their entire lifetime to this window.
    rows = [read(p.parent.name) for p in root.glob("*/stats.json")]
    rows = [r for r in rows if r.get("started_at", 0) >= cutoff]
    totals = {key: sum(row.get(key, 0) for row in rows) for key in COUNTERS}
    return {
        "current_session": read(session_key) if session_key else {},
        "sessions_started_in_last_days": days,
        "sessions": len(rows),
        "totals": totals,
        "note": "Digest items used counts delivery, not proven use in an answer.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default="")
    parser.add_argument("--days", default=7, type=int)
    args = parser.parse_args()
    print(json.dumps(report(args.session, max(1, args.days)), indent=2))
