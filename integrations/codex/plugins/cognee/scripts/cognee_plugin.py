"""cognee-plugin — CLI utility for the cognee Claude-Code / Codex plugin.

Usage:
    cognee-plugin metrics [--json] [--plugin <claude-code|codex>]

Computes usage rollup purely from local files — no network required:
    ~/.cognee-plugin/<plugin>/hook.log
    ~/.cognee-plugin/<plugin>/save_counter.json
    ~/.cognee-plugin/<plugin>/last_recall.json
    ~/.cognee-plugin/<plugin>/recall-audit.log
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


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
    hook_log_path     = plugin_dir / "hook.log"
    save_counter_path = plugin_dir / "save_counter.json"
    last_recall_path  = plugin_dir / "last_recall.json"
    recall_audit_path = plugin_dir / "recall-audit.log"

    # -----------------------------------------------------------------------
    # 1. hook.log — sessions, mode split, breaker trips, save events
    # -----------------------------------------------------------------------
    hook_lines = _parse_jsonl(hook_log_path)

    unique_sessions: set = set()
    local_decisions = 0
    cloud_decisions = 0
    breaker_trips = 0
    saves_from_log = {"prompt": 0, "trace": 0, "answer": 0}

    for line in hook_lines:
        ev = line.get("event", "")
        detail = line.get("detail") or {}

        # Collect session ids from any log line
        for key in ("session_id", "session"):
            sid = detail.get(key) or line.get(key)
            if sid and isinstance(sid, str):
                unique_sessions.add(sid)

        # Mode decisions
        if ev == "mode_decision":
            mode = detail.get("mode", "")
            if mode == "http":
                cloud_decisions += 1
            elif mode in ("local", "loopback"):
                local_decisions += 1

        # Breaker trips recorded via hook_log
        if ev == "recall_breaker_open":
            breaker_trips += 1

        # Save events that appear in hook.log
        if ev == "prompt_pending":
            saves_from_log["prompt"] += 1
        if ev == "trace_stored":
            saves_from_log["trace"] += 1
        if ev == "stop_stored":
            saves_from_log["answer"] += 1

    # -----------------------------------------------------------------------
    # 2. save_counter.json — live per-session save counts
    # -----------------------------------------------------------------------
    save_data = _read_json_file(save_counter_path)
    saves_counter = {"prompt": 0, "trace": 0, "answer": 0}
    for _sid, counts in save_data.items():
        if isinstance(counts, dict):
            for kind in ("prompt", "trace", "answer"):
                saves_counter[kind] += int(counts.get(kind, 0))
        
        # In case a session exists in save_counter but wasn't in hook.log
        if isinstance(_sid, str):
            unique_sessions.add(_sid)

    # Prefer log-derived totals (historical) and supplement with live counter
    total_saves = {
        kind: saves_from_log[kind] + saves_counter[kind]
        for kind in ("prompt", "trace", "answer")
    }

    # -----------------------------------------------------------------------
    # 3. last_recall.json — most-recent recall hit counts
    # -----------------------------------------------------------------------
    last_recall = _read_json_file(last_recall_path)
    last_hits = last_recall.get("hits") or {}
    last_recall_ts = last_recall.get("ts", "")
    if last_recall.get("session_id"):
        unique_sessions.add(last_recall.get("session_id"))

    # -----------------------------------------------------------------------
    # 4. recall-audit.log — total recalls + hit rate
    # -----------------------------------------------------------------------
    audit_lines = _parse_jsonl(recall_audit_path)
    total_recalls = len(audit_lines)
    recall_hits = 0
    for entry in audit_lines:
        hits = entry.get("hits") or {}
        total_hit_count = sum(int(v) for v in hits.values() if isinstance(v, (int, float)))
        if total_hit_count > 0:
            recall_hits += 1
        
        # Extract session ID from audit logs as well
        sid = entry.get("session_id")
        if sid and isinstance(sid, str):
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
        "circuit_breaker": {
            "trips": breaker_trips,
        },
        "last_recall": {
            "ts": last_recall_ts,
            "hits": last_hits,
        },
    }


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def _print_rollup(metrics: dict, plugin: str) -> None:
    m = metrics
    r = m["recalls"]
    s = m["saves"]
    ms = m["mode_split"]
    cb = m["circuit_breaker"]
    lr = m["last_recall"]

    print("cognee-plugin metrics  [{}]".format(plugin))
    print("=" * 46)
    print("  Sessions            : {}".format(m["sessions"]))
    print()
    print("  Recalls             : {}".format(r["total"]))
    print("  Recall hits         : {}".format(r["hits"]))
    print("  Hit rate            : {}%".format(r["hit_rate_pct"]))
    if lr.get("ts"):
        hits_str = "  ".join(
            "{}={}".format(k, v) for k, v in (lr.get("hits") or {}).items()
        )
        print("  Last recall ts      : {}".format(lr["ts"]))
        if hits_str:
            print("  Last recall hits    : {}".format(hits_str))
    print()
    print("  Saves (prompt)      : {}".format(s["prompt"]))
    print("  Saves (trace)       : {}".format(s["trace"]))
    print("  Saves (answer)      : {}".format(s["answer"]))
    print("  Saves (total)       : {}".format(sum(s.values())))
    print()
    print("  Mode — local        : {}%  ({} decisions)".format(ms["local_pct"], ms["local_count"]))
    print("  Mode — cloud        : {}%  ({} decisions)".format(ms["cloud_pct"], ms["cloud_count"]))
    print()
    print("  Breaker trips       : {}".format(cb["trips"]))
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
    metrics_p.add_argument(
        "--plugin",
        default="claude-code",
        choices=["claude-code", "codex"],
        help="Which plugin state directory to read (default: claude-code).",
    )
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return 1

    if args.command == "metrics":
        plugin_name = args.plugin
        if plugin_name == "codex":
            plugin_dir = Path.home() / ".cognee-plugin" / "codex"
        else:
            plugin_dir = Path.home() / ".cognee-plugin" / "claude-code"

        metrics = _compute_metrics(plugin_dir)

        if args.as_json:
            print(json.dumps(metrics, indent=2))
        else:
            _print_rollup(metrics, plugin_name)

        return 0

    parser.print_help(sys.stderr)
    return 1

if __name__ == "__main__":
    sys.exit(main())
