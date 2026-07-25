"""cognee-plugin - offline usage-metrics CLI for the cognee Claude Code plugin.

Usage:
    cognee-plugin metrics [--json]

Computes a usage rollup purely from this plugin's local state files - no
network, no import of the cognee package:
    ~/.cognee-plugin/claude-code/hook.log
    ~/.cognee-plugin/claude-code/save_counter.json
    ~/.cognee-plugin/claude-code/last_recall.json
    ~/.cognee-plugin/claude-code/recall-audit.log
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# State this plugin's hooks write (mirrors _plugin_common._PLUGIN_DIR). The
# codex copy of this file points at ".../codex"; each copy reads its own dir.
_PLUGIN_DIR = Path.home() / ".cognee-plugin" / "claude-code"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_jsonl(path: Path) -> list:
    """Read a JSON-Lines file; skip malformed lines silently."""
    if not path.exists():
        return []
    lines = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    lines.append(obj)
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return lines


def _read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------


def _compute_metrics(plugin_dir: Path) -> dict:
    hook_log_path = plugin_dir / "hook.log"
    save_counter_path = plugin_dir / "save_counter.json"
    last_recall_path = plugin_dir / "last_recall.json"
    recall_audit_path = plugin_dir / "recall-audit.log"

    # -----------------------------------------------------------------------
    # 1. hook.log - sessions, mode split, breaker events, save events
    # -----------------------------------------------------------------------
    hook_lines = _parse_jsonl(hook_log_path)

    unique_sessions: set = set()
    local_decisions = 0
    cloud_decisions = 0
    breaker_open_events = 0
    saves_from_log = {"prompt": 0, "trace": 0, "answer": 0}

    for line in hook_lines:
        ev = line.get("event", "")
        detail = line.get("detail") or {}

        # Some diagnostic events carry a session id in their detail payload.
        for key in ("session_id", "session"):
            sid = detail.get(key)
            if isinstance(sid, str) and sid:
                unique_sessions.add(sid)

        # Mode decisions. resolve_runtime_mode() emits "http" (cloud) or
        # "local_sdk" (local); count any non-http mode as local so the split
        # stays correct if another local mode name is ever added.
        if ev == "mode_decision":
            mode = detail.get("mode", "")
            if mode == "http":
                cloud_decisions += 1
            elif mode:
                local_decisions += 1

        # recall_breaker_open is logged once per per-prompt recall skipped while
        # the breaker is open; the 4 local files expose no open/close transition,
        # so this counts recalls skipped by an open breaker, not distinct trips.
        if ev == "recall_breaker_open":
            breaker_open_events += 1

        # Save events recorded in hook.log. Warmup-buffered trace/answer saves
        # log "store_buffered_warming" (tagged with the originating hook) rather
        # than trace_stored/stop_stored, so count those too for a full total.
        if ev == "prompt_pending":
            saves_from_log["prompt"] += 1
        elif ev == "trace_stored":
            saves_from_log["trace"] += 1
        elif ev == "stop_stored":
            saves_from_log["answer"] += 1
        elif ev == "store_buffered_warming":
            if detail.get("hook") == "tool":
                saves_from_log["trace"] += 1
            elif detail.get("hook") == "stop":
                saves_from_log["answer"] += 1

    # -----------------------------------------------------------------------
    # 2. save_counter.json - session ids only
    # -----------------------------------------------------------------------
    # A per-turn buffer that read_and_reset_save_counter drains on every recall;
    # each save it records is also written to hook.log, so adding it to the
    # totals would double-count. Read it only to recover session ids.
    save_data = _read_json_file(save_counter_path)
    for _sid in save_data:
        if isinstance(_sid, str):
            unique_sessions.add(_sid)

    total_saves = dict(saves_from_log)

    # -----------------------------------------------------------------------
    # 3. last_recall.json - session id only
    # -----------------------------------------------------------------------
    lr_session = _read_json_file(last_recall_path).get("session_id")
    if isinstance(lr_session, str) and lr_session:
        unique_sessions.add(lr_session)

    # -----------------------------------------------------------------------
    # 4. recall-audit.log - total recalls + hit rate
    # -----------------------------------------------------------------------
    audit_lines = _parse_jsonl(recall_audit_path)
    total_recalls = len(audit_lines)
    recall_hits = 0
    for entry in audit_lines:
        hits = entry.get("hits") or {}
        total_hit_count = sum(int(v) for v in hits.values() if isinstance(v, (int, float)))
        if total_hit_count > 0:
            recall_hits += 1
        sid = entry.get("session_id")
        if isinstance(sid, str) and sid:
            unique_sessions.add(sid)

    hit_rate_pct = round(100.0 * recall_hits / total_recalls, 1) if total_recalls > 0 else 0.0

    total_sessions = len(unique_sessions)
    total_decisions = local_decisions + cloud_decisions
    local_pct = round(100.0 * local_decisions / total_decisions, 1) if total_decisions > 0 else 0.0
    cloud_pct = round(100.0 * cloud_decisions / total_decisions, 1) if total_decisions > 0 else 0.0

    return {
        "sessions": total_sessions,
        "recalls": {
            "total": total_recalls,
            "hits": recall_hits,
            "hit_rate_pct": hit_rate_pct,
        },
        "saves": total_saves,
        "mode_split": {
            "local_pct": local_pct,
            "cloud_pct": cloud_pct,
            "local_count": local_decisions,
            "cloud_count": cloud_decisions,
        },
        "breaker_open_events": breaker_open_events,
    }


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


def _print_rollup(metrics: dict, plugin: str) -> None:
    r = metrics["recalls"]
    s = metrics["saves"]
    ms = metrics["mode_split"]

    print(f"cognee-plugin metrics  [{plugin}]")
    print("=" * 46)
    print(f"  Sessions            : {metrics['sessions']}")
    print()
    print(f"  Recalls             : {r['total']}")
    print(f"  Recall hits         : {r['hits']}")
    print(f"  Hit rate            : {r['hit_rate_pct']}%")
    print()
    print(f"  Saves (prompt)      : {s['prompt']}")
    print(f"  Saves (trace)       : {s['trace']}")
    print(f"  Saves (answer)      : {s['answer']}")
    print(f"  Saves (total)       : {sum(s.values())}")
    print()
    print(f"  Mode - local        : {ms['local_pct']}%  ({ms['local_count']} decisions)")
    print(f"  Mode - cloud        : {ms['cloud_pct']}%  ({ms['cloud_count']} decisions)")
    print()
    print(f"  Breaker open events : {metrics['breaker_open_events']}")
    print("=" * 46)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cognee-plugin",
        description="Cognee plugin CLI utilities",
    )
    sub = parser.add_subparsers(dest="command")

    metrics_p = sub.add_parser(
        "metrics",
        help="Print a usage rollup derived purely from local plugin files (no network).",
    )
    metrics_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Output metrics as a JSON object instead of a human-readable rollup.",
    )
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return 1

    if args.command == "metrics":
        metrics = _compute_metrics(_PLUGIN_DIR)
        if args.as_json:
            print(json.dumps(metrics, indent=2))
        else:
            _print_rollup(metrics, _PLUGIN_DIR.name)
        return 0

    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
