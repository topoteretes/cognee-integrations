#!/usr/bin/env python3
"""Evaluate the Cognee plugin from the telemetry it already writes.

Reads, for a chosen time window:

* ``hook.log`` (+ rotations)      - what the hooks attempted and how it went
* ``recall-audit.log``            - every recall: hits per scope, injected context
* Claude Code transcripts         - hook durations/crashes, token usage, skill use
* ``installed_plugins.json``      - installed version and when it was last updated
* optionally the Cognee server    - what actually landed (``--server``)

and produces a scorecard (JSON / text / Markdown), an objective grade with
explicit gates, and evidence-backed recommendations. Everything here is
deterministic: the numbers come from this file, never from a model.

Only aggregates leave this process. Prompt and memory text is read to measure
repetition but is never written to a report or exported.

Stdlib only, Python 3.9+, so it runs under the same ``python3`` the hooks use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

SCHEMA_VERSION = 1

# Recall context is split into items (one trace step, one Q/A paragraph). Items
# shorter than this are headers and separators, not memory.
MIN_ITEM_CHARS = 40
PROBE_CHARS = 40
CHARS_PER_TOKEN = 4.0  # estimate; labelled as such wherever it is shown
MAX_TRANSCRIPT_CHUNK = 200_000

# (key, sub-score, label, direction, target, gate, floor, min_n, unit)
#   target -> full marks, floor -> zero marks, gate -> must be at least this good
#   min_n  -> below this many observations the metric is shown but not scored
METRIC_SPECS: List[Tuple[str, str, str, str, float, float, float, int, str]] = [
    (
        "store_landed_rate",
        "reliability",
        "Writes that landed",
        "higher",
        0.99,
        0.95,
        0.80,
        20,
        "pct",
    ),
    (
        "improve_ok_rate",
        "reliability",
        "Improve runs that succeeded",
        "higher",
        0.95,
        0.80,
        0.40,
        5,
        "pct",
    ),
    (
        "recall_error_rate",
        "reliability",
        "Recalls with a failed scope",
        "lower",
        0.01,
        0.05,
        0.20,
        20,
        "pct",
    ),
    (
        "hook_crash_rate",
        "reliability",
        "Plugin hook runs that crashed",
        "lower",
        0.0,
        0.02,
        0.20,
        10,
        "pct",
    ),
    ("hit_rate", "usefulness", "Recalls returning a hit", "higher", 0.85, 0.60, 0.30, 20, "pct"),
    (
        "novelty_rate",
        "usefulness",
        "Injected text new to the session",
        "higher",
        0.80,
        0.50,
        0.10,
        20,
        "pct",
    ),
    (
        "graph_hit_share",
        "usefulness",
        "Hits from the knowledge graph",
        "higher",
        0.25,
        0.10,
        0.0,
        50,
        "pct",
    ),
    (
        "median_injected_tokens",
        "cost",
        "Median tokens injected per recall",
        "lower",
        500,
        1500,
        4000,
        20,
        "tokens",
    ),
    (
        "overhead_share",
        "cost",
        "Injected tokens vs fresh model input",
        "lower",
        0.03,
        0.10,
        0.30,
        3,
        "pct",
    ),
    (
        "empty_injection_rate",
        "cost",
        "Injections made with zero hits",
        "lower",
        0.0,
        0.10,
        0.50,
        20,
        "pct",
    ),
    ("recall_p95_ms", "latency", "Recall hook p95", "lower", 1500, 3000, 10000, 10, "ms"),
    (
        "session_start_p95_ms",
        "latency",
        "SessionStart hook p95",
        "lower",
        3000,
        8000,
        30000,
        3,
        "ms",
    ),
    (
        "top_event_share",
        "hygiene",
        "hook.log share taken by one event",
        "lower",
        0.30,
        0.60,
        0.95,
        200,
        "pct",
    ),
    ("largest_log_mb", "hygiene", "Largest plugin log file", "lower", 25, 100, 1000, 1, "mb"),
]
SUBSCORES = ["reliability", "usefulness", "cost", "latency", "hygiene"]
MIN_SUBSCORES_FOR_GRADE = 3
LETTERS = [(90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "F")]

_STORE_OK = {"stop_stored"}
_STORE_BUFFERED = {
    "store_buffered_warming",
    "store_buffered_after_error",
    "trace_buffered_after_error",
}
_STORE_ERRORS = {"stop_store_error", "trace_store_error", "trace_store_noresult"}
_DRAIN_PROBLEMS = {"warmup_drain_error", "warmup_drain_failed", "warmup_drain_budget_exceeded"}
_RECALL_PROBLEMS = {
    "recall_error",
    "recall_budget_exceeded",
    "recall_breaker_open",
    "recall_server_down",
    "recall_auth_rejected",
    "recall_skipped_not_ready",
    "recall_graph_not_built",
    "context_lookup_exception",
}
_CONNECTIVITY = {
    "runtime_state_users_me_failed",
    "runtime_state_connection_lookup_failed",
    "credits_fetch_failed",
    "agent_register_failed",
    "dataset_ensure_failed",
}
_FAILURE_SUFFIXES = ("_failed", "_error", "_exception", "_crashed")
_HOOK_STATUS_MESSAGES = (
    "initializing cognee memory",
    "searching session memory",
    "clearing claude context",
    "building memory anchor",
)
# A hook with a statusMessage is logged under that message, while its error names the
# script path - so both have to resolve to the same hook.
_STATUS_TO_SCRIPT = {
    "initializing cognee memory": "session-start.py",
    "searching session memory": "session-context-lookup.py",
    "clearing claude context": "clear-transcript-context.py",
    "building memory anchor": "pre-compact.py",
}
_COGNEE_HOOK_SCRIPTS = (
    "store-to-session.py",
    "store-user-prompt.py",
    "session-context-lookup.py",
    "clear-transcript-context.py",
    "credits-refresh.py",
    "sync-session-to-graph.py",
)
_SCRIPT_NAME_RE = re.compile(r"scripts/([\w.\-]+\.(?:py|sh))")
_PLUGIN_VERSION_RE = re.compile(r"cognee-memory/(\d[\w.\-]*)/")
_PLUGIN_SCRIPT_RE = re.compile(
    r"(?:\$\{?CLAUDE_PLUGIN_ROOT\}?|/scripts)/"
    r"(cognee-search\.sh|cognee-remember\.sh|cognee-doctor\.sh|cognee-plugin|sync-session-to-graph\.py)(?![\w.\-])"
)
_RUNNERS = ("bash ", "sh ", "python3 ", "python ", "$", "/", "~")
_BOILERPLATE_PREFIXES = ("relevant context from", "cognee memory:", "===")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def redact(value: Any, limit: int = 200) -> str:
    """Stringify for a report: home directory collapsed, length bounded."""
    text = str(value if value is not None else "")
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse a log timestamp to aware UTC. Naive stamps are UTC (that is how hooks write them)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # 3.9's fromisoformat accepts only 3 or 6 fractional digits.
    match = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", text)
    if match:
        frac = match.group(2) or ""
        if frac:
            frac = "." + (frac[1:] + "000000")[:6]
        text = match.group(1) + frac + match.group(3)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds") if moment else None


def to_local_label(moment: Optional[datetime]) -> str:
    if moment is None:
        return "-"
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


class WhenError(ValueError):
    pass


_RELATIVE_RE = re.compile(r"^(\d+)\s*(m|min|mins|h|hr|hrs|d|day|days|w|wk|wks)$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_when(
    text: Optional[str], now: datetime, install: Dict[str, Any], end_of_day: bool = False
) -> Optional[datetime]:
    """Resolve a user-supplied date. Naive input is LOCAL time (the user's wall clock).

    Accepts: ``all``; ``last-update`` / ``installed`` (from Claude Code's plugin
    registry); ``today`` / ``yesterday``; relative ``90m`` ``24h`` ``7d`` ``2w``;
    ``YYYY-MM-DD``; any ISO datetime. A date-only ``--until`` means the end of that day.
    """
    if text is None:
        return None
    raw = text.strip().lower()
    if raw in ("", "all", "all-time", "alltime", "none"):
        return None
    if raw in ("last-update", "lastupdate", "since-update", "update", "updated"):
        moment = parse_ts(install.get("last_updated"))
        if moment is None:
            raise WhenError(
                "cannot resolve 'last-update': no cognee entry with lastUpdated in "
                "~/.claude/plugins/installed_plugins.json. Pass an explicit date instead."
            )
        return moment
    if raw in ("installed", "install", "first-install"):
        moment = parse_ts(install.get("installed_at"))
        if moment is None:
            raise WhenError(
                "cannot resolve 'installed': install time unknown. Pass an explicit date."
            )
        return moment
    local_now = now.astimezone()
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if raw == "now":
        return now
    if raw == "today":
        return (midnight + timedelta(days=1) if end_of_day else midnight).astimezone(timezone.utc)
    if raw == "yesterday":
        day = midnight - timedelta(days=1)
        return (day + timedelta(days=1) if end_of_day else day).astimezone(timezone.utc)
    relative = _RELATIVE_RE.match(raw)
    if relative:
        seconds = int(relative.group(1)) * _UNIT_SECONDS[relative.group(2)[0]]
        return now - timedelta(seconds=seconds)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        try:
            day = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError as exc:
            raise WhenError("not a valid date: %s" % text) from exc
        if end_of_day:
            day += timedelta(days=1)
        return day.astimezone().astimezone(timezone.utc)  # naive -> local -> UTC
    candidate = text.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(
            candidate.replace(" ", "T", 1) if "T" not in candidate else candidate
        )
    except ValueError as exc:
        raise WhenError(
            "could not read %r. Use YYYY-MM-DD, an ISO datetime, a relative span "
            "(90m, 24h, 7d, 2w), today, yesterday, last-update, installed or all." % text
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # naive -> local
    return parsed.astimezone(timezone.utc)


def quantile(values: List[float], q: float) -> Optional[float]:
    """Nearest-rank quantile; None on no data."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return float(ordered[index])


def ratio(numerator: float, denominator: float) -> Optional[float]:
    return (numerator / denominator) if denominator else None


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def rotated_files(directory: Path, base: str) -> List[Path]:
    """``base`` and its numbered rotations, oldest first. Ignores ``.lock`` and friends."""
    found: List[Tuple[int, Path]] = []
    try:
        candidates = list(directory.glob(base + "*"))
    except OSError:
        return []
    for path in candidates:
        if path.name == base:
            found.append((0, path))
            continue
        match = re.match(r"^\.(\d+)$", path.name[len(base) :])
        if match:
            found.append((int(match.group(1)), path))
    return [path for _idx, path in sorted(found, key=lambda pair: -pair[0])]


_TS_PREFIX_RE = re.compile(r'"ts"\s*:\s*"([^"]+)"')


def iter_jsonl(
    paths: List[Path], stats: Dict[str, Any]
) -> Iterator[Tuple[Optional[datetime], str]]:
    """Yield ``(ts, raw_line)`` for every line, reading the stamp without a full parse."""
    for path in paths:
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if not line.strip():
                    continue
                stats["lines"] = stats.get("lines", 0) + 1
                match = _TS_PREFIX_RE.search(line, 0, 160)
                yield (parse_ts(match.group(1)) if match else None), line


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def load_install_info(claude_dir: Path) -> Dict[str, Any]:
    """Installed cognee plugin version + timestamps, from Claude Code's own registry."""
    data = read_json(claude_dir / "plugins" / "installed_plugins.json")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    best: Dict[str, Any] = {}
    best_stamp: Optional[datetime] = None
    for key, entries in (plugins or {}).items():
        if "cognee" not in str(key).lower():
            continue
        for entry in entries if isinstance(entries, list) else [entries]:
            if not isinstance(entry, dict):
                continue
            updated = parse_ts(entry.get("lastUpdated"))
            if best and updated and best_stamp and updated <= best_stamp:
                continue
            best_stamp = updated or best_stamp
            best = {
                "plugin": str(key),
                "version": entry.get("version"),
                "installed_at": to_iso(parse_ts(entry.get("installedAt"))),
                "last_updated": to_iso(updated),
                "git_sha": str(entry.get("gitCommitSha") or "")[:12],
            }
    return best


def analyze_hook_log(
    plugin_dir: Path, since: Optional[datetime], until: Optional[datetime]
) -> Dict[str, Any]:
    stats: Dict[str, Any] = {}
    events: Counter = Counter()
    failures: Counter = Counter()
    improve_errors: Counter = Counter()
    improve_triggers: Counter = Counter()
    recall_verdicts: Counter = Counter()
    modes: Counter = Counter()
    advanced: Counter = Counter()
    lookup_ms: List[float] = []
    out: Dict[str, int] = defaultdict(int)
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    update_check: Dict[str, Any] = {}
    unparseable = 0
    errored_recalls: set = set()
    budget_recalls: set = set()

    for ts, line in iter_jsonl(rotated_files(plugin_dir, "hook.log"), stats):
        if ts is not None:
            first_ts = ts if first_ts is None or ts < first_ts else first_ts
            last_ts = ts if last_ts is None or ts > last_ts else last_ts
            if (since and ts < since) or (until and ts >= until):
                continue
        try:
            record = json.loads(line)
        except ValueError:
            unparseable += 1
            continue
        if not isinstance(record, dict):
            continue
        if ts is None:
            ts = parse_ts(record.get("ts"))
            if ts is None or (since and ts < since) or (until and ts >= until):
                continue
        event = str(record.get("event") or "")
        detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
        events[event] += 1

        if event == "trace_stored":
            out[
                "stored" if str(detail.get("status", "success")) == "success" else "store_errors"
            ] += 1
        elif event in _STORE_OK:
            out["stored"] += 1
        elif event in _STORE_BUFFERED:
            out["buffered"] += 1
        elif event in _STORE_ERRORS:
            out["store_errors"] += 1
        elif event == "warmup_drained":
            try:
                out["drained"] += int(detail.get("count") or 0)
            except (TypeError, ValueError):
                pass
        elif event in _DRAIN_PROBLEMS:
            out["drain_problems"] += 1
        elif event == "improve_fired":
            improve_triggers[str(detail.get("trigger") or "unknown")] += 1
            if detail.get("busy"):
                out["improve_busy"] += 1
            elif detail.get("ok"):
                out["improve_ok"] += 1
            else:
                out["improve_failed"] += 1
                improve_errors[
                    redact(detail.get("error") or detail.get("reason") or "no error text", 90)
                ] += 1
        elif event == "auto_improve_fired":
            improve_triggers["auto"] += 1
            key = "improve_ok" if (detail.get("wrote") or detail.get("ok")) else "improve_failed"
            out[key] += 1
            if key == "improve_failed":
                improve_errors["auto improve did not write"] += 1
        elif event == "auto_improve_error":
            out["improve_failed"] += 1
            improve_errors[redact(detail.get("error") or "auto improve error", 90)] += 1
        elif event == "sync_bridge_done":
            out["sync_wrote" if detail.get("wrote") else "sync_not_written"] += 1
        elif event == "sync_failed":
            out["sync_failed"] += 1
        elif event in ("context_lookup_hit", "context_lookup_empty"):
            out["lookups"] += 1
            elapsed = detail.get("elapsed_ms")
            if isinstance(elapsed, (int, float)) and elapsed >= 0:
                lookup_ms.append(float(elapsed))
        elif event == "mode_decision":
            host = urllib.parse.urlparse(str(detail.get("base_url") or "")).hostname or ""
            modes["local" if host in ("localhost", "127.0.0.1", "::1", "") else "remote"] += 1
        elif event == "update_check":
            update_check = {k: detail.get(k) for k in ("installed", "latest", "available")}

        if event in _RECALL_PROBLEMS:
            out["recall_problems"] += 1
            # One hook process serves one recall, so (pid, hour) identifies the recall
            # even when several scopes of it fail.
            recall_key = (record.get("pid"), ts.strftime("%Y%m%d%H"))
            if event == "recall_error":
                out["recall_errors"] += 1
                errored_recalls.add(recall_key)
                recall_verdicts[str(detail.get("verdict") or "unknown")] += 1
            elif event == "recall_budget_exceeded":
                budget_recalls.add(recall_key)
        if event == "trace_skipped_low_signal":
            out["low_signal_skipped"] += 1
        if event == "skip_self_cognee_bash":
            out["self_skipped"] += 1
        if event in _CONNECTIVITY:
            out["connectivity_failures"] += 1
        if event == "credits_payment_required":
            out["payment_required"] += 1
        if event.startswith("switch_") and not event.endswith(
            _FAILURE_SUFFIXES + ("_aborted_handle_cleanup",)
        ):
            advanced["dataset_switch"] += 1
        elif event.startswith("shared_memory_") and not event.endswith(
            _FAILURE_SUFFIXES + ("_skipped",)
        ):
            advanced["shared_memory"] += 1
        if event.endswith(_FAILURE_SUFFIXES):
            failures[event] += 1

    total = sum(events.values())
    top_event, top_count = events.most_common(1)[0] if events else ("", 0)
    writes = out["stored"] + out["buffered"] + out["store_errors"]
    landed = min(writes, out["stored"] + out["drained"])
    improves = out["improve_ok"] + out["improve_failed"]
    return {
        "lines_in_window": total,
        "unparseable_lines": unparseable,
        "first_ts": to_iso(first_ts),
        "last_ts": to_iso(last_ts),
        "distinct_events": len(events),
        "top_event": top_event,
        "top_event_share": ratio(top_count, total),
        "stores": {
            "stored_directly": out["stored"],
            "buffered": out["buffered"],
            "errors": out["store_errors"],
            "drained_later": out["drained"],
            "drain_problems": out["drain_problems"],
            "attempted": writes,
            "landed_rate": ratio(landed, writes),
            "buffered_share": ratio(out["buffered"], writes),
            "skipped_as_self_reference": out["self_skipped"],
            "skipped_as_low_signal": out["low_signal_skipped"],
            "self_skip_share": ratio(out["self_skipped"], out["self_skipped"] + writes),
        },
        "improve": {
            "ok": out["improve_ok"],
            "failed": out["improve_failed"],
            "busy": out["improve_busy"],
            "ok_rate": ratio(out["improve_ok"], improves),
            "runs": improves,
            "triggers": dict(improve_triggers),
            "top_errors": improve_errors.most_common(3),
        },
        "sync": {
            "wrote": out["sync_wrote"],
            "not_written": out["sync_not_written"],
            "failed": out["sync_failed"],
        },
        "recall_events": {
            "lookups_logged": out["lookups"],
            "problems": out["recall_problems"],
            "errors": out["recall_errors"],
            "verdicts": dict(recall_verdicts),
            "recalls_with_error": len(errored_recalls),
            "recalls_over_budget": len(budget_recalls),
            "lookup_ms": lookup_ms,
        },
        "connectivity_failures": out["connectivity_failures"],
        "payment_required": out["payment_required"],
        "mode_split": dict(modes),
        "advanced_usage": dict(advanced),
        "update_check": update_check,
        "top_failures": failures.most_common(6),
    }


def _hit_total(hits: Any) -> int:
    if isinstance(hits, dict):
        return sum(
            int(v) for v in hits.values() if isinstance(v, (int, float)) and not isinstance(v, bool)
        )
    if isinstance(hits, (int, float)) and not isinstance(hits, bool):
        return int(hits)
    return len(hits) if isinstance(hits, list) else 0


def _is_graph_scope(scope: str) -> bool:
    return scope.startswith("graph") or scope == "code"


_ITEM_SPLIT_RE = re.compile(r"\n\s*\n|\n(?=\[[^\]\n]{2,40}\] )")
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Comparable form: even out escapes that differ between a transcript and its trace summary."""
    text = text.replace("\\n", " ").replace("\\t", " ").replace('\\"', '"')
    return _WS_RE.sub(" ", text).strip().lower()


def split_items(context: str) -> List[str]:
    items = []
    for part in _ITEM_SPLIT_RE.split(context or ""):
        normalized = normalize_text(part)
        if len(normalized) < MIN_ITEM_CHARS or normalized.startswith(_BOILERPLATE_PREFIXES):
            continue
        items.append(normalized)
    return items


def item_probes(item: str) -> List[str]:
    """Three short slices from the body of an item (headers live at the start)."""
    if len(item) < PROBE_CHARS * 1.5:
        return []
    probes = []
    for fraction in (0.25, 0.5, 0.75):
        start = max(0, min(len(item) - PROBE_CHARS, int(len(item) * fraction) - PROBE_CHARS // 2))
        probes.append(item[start : start + PROBE_CHARS])
    return probes


def analyze_recalls(
    plugin_dir: Path, since: Optional[datetime], until: Optional[datetime], session: Optional[str]
) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
    """Recall metrics, plus per-session in-window recalls for the transcript pass."""
    stats: Dict[str, Any] = {}
    seen: Dict[str, set] = defaultdict(set)
    scope_hits: Counter = Counter()
    scope_ms: Dict[str, List[float]] = defaultdict(list)
    slowest_scope_ms: List[float] = []
    injected_chars: List[int] = []
    per_session: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    counts: Dict[str, float] = defaultdict(float)
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    days = set()

    for ts, line in iter_jsonl(rotated_files(plugin_dir, "recall-audit.log"), stats):
        if ts is None or (until and ts >= until):
            continue
        first_ts = ts if first_ts is None or ts < first_ts else first_ts
        last_ts = ts if last_ts is None or ts > last_ts else last_ts
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        sid = str(row.get("session_id") or "")
        if session and session not in (sid, sid.split("_", 1)[-1]):
            continue
        context = row.get("context") if isinstance(row.get("context"), str) else ""
        items = split_items(context)
        in_window = not (since and ts < since)

        repeated_chars = new_chars = repeated_items = 0
        hashes = []
        for item in items:
            digest = hashlib.sha1(item.encode("utf-8", "replace")).digest()[:10]
            hashes.append(digest)
            if digest in seen[sid]:
                repeated_items += 1
                repeated_chars += len(item)
            else:
                new_chars += len(item)
        seen[sid].update(hashes)  # rows before the window still prime "already injected"
        if not in_window:
            continue

        hits = _hit_total(row.get("hits"))
        counts["recalls"] += 1
        counts["with_hits"] += 1 if hits > 0 else 0
        if hits == 0 and context.strip():
            counts["empty_injections"] += 1
            counts["empty_injection_chars"] += len(context)
        counts["items"] += len(items)
        counts["repeated_items"] += repeated_items
        counts["repeated_chars"] += repeated_chars
        counts["new_chars"] += new_chars
        if items and repeated_items == len(items):
            counts["fully_repeated"] += 1
        measured_chars = row.get("context_chars")
        injected_chars.append(
            measured_chars
            if isinstance(measured_chars, int) and measured_chars >= 0
            else len(context)
        )
        days.add(ts.astimezone().date())
        if isinstance(row.get("hits"), dict):
            for scope, value in row["hits"].items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    scope_hits[str(scope)] += int(value)
        slowest = 0.0
        if isinstance(row.get("per_scope"), dict):
            for scope, info in row["per_scope"].items():
                if not isinstance(info, dict):
                    continue
                if info.get("skipped"):
                    counts["graph_skipped"] += 1 if _is_graph_scope(str(scope)) else 0
                    continue
                elapsed = info.get("elapsed_ms")
                if isinstance(elapsed, (int, float)) and elapsed > 0:
                    scope_ms[str(scope)].append(float(elapsed))
                    slowest = max(slowest, float(elapsed))
        if slowest:
            slowest_scope_ms.append(slowest)
        per_session[sid].append(
            {
                "ts": ts,
                "items": items,
                "chars": len(context),
                "hits": hits,
                "new_chars": new_chars,
                "repeated_chars": repeated_chars,
                "graph_hits": sum(
                    v
                    for k, v in (row.get("hits") or {}).items()
                    if _is_graph_scope(k) and isinstance(v, (int, float))
                )
                if isinstance(row.get("hits"), dict)
                else 0,
                "entries_kept": row.get("entries_kept", len(items)),
                "entries_deduped": row.get("entries_deduped", 0),
                "hashes": [h.hex() for h in hashes],
            }
        )

    recalls = int(counts["recalls"])
    total_hits = sum(scope_hits.values())
    graph_hits = sum(v for k, v in scope_hits.items() if _is_graph_scope(k))
    body_chars = counts["repeated_chars"] + counts["new_chars"]
    total_chars = sum(injected_chars)
    result = {
        "recalls": recalls,
        "sessions": len(per_session),
        "active_days": len(days),
        "first_ts": to_iso(first_ts),
        "last_ts": to_iso(last_ts),
        "hit_rate": ratio(counts["with_hits"], recalls),
        "scope_hits": dict(scope_hits.most_common()),
        "total_hits": total_hits,
        "graph_hit_share": ratio(graph_hits, total_hits),
        "graph_skipped_rate": ratio(counts["graph_skipped"], recalls),
        "injected_chars": total_chars,
        "injected_tokens_est": int(total_chars / CHARS_PER_TOKEN),
        "median_injected_tokens": (quantile([c / CHARS_PER_TOKEN for c in injected_chars], 0.5)),
        "p90_injected_tokens": (quantile([c / CHARS_PER_TOKEN for c in injected_chars], 0.9)),
        "max_injected_tokens": (max(injected_chars) / CHARS_PER_TOKEN if injected_chars else None),
        "empty_injection_rate": ratio(counts["empty_injections"], recalls),
        "empty_injection_tokens_est": int(counts["empty_injection_chars"] / CHARS_PER_TOKEN),
        "reinjection": {
            "items": int(counts["items"]),
            "repeated_items": int(counts["repeated_items"]),
            "repeated_item_share": ratio(counts["repeated_items"], counts["items"]),
            "repeated_char_share": ratio(counts["repeated_chars"], body_chars),
            "repeated_tokens_est": int(counts["repeated_chars"] / CHARS_PER_TOKEN),
            "fully_repeated_recalls": int(counts["fully_repeated"]),
        },
        "novelty_rate": (1.0 - counts["repeated_chars"] / body_chars) if body_chars else None,
        "scope_latency_ms": {
            scope: {"n": len(v), "p50": quantile(v, 0.5), "p95": quantile(v, 0.95)}
            for scope, v in sorted(scope_ms.items())
        },
        "slowest_scope_ms": {
            "n": len(slowest_scope_ms),
            "p50": quantile(slowest_scope_ms, 0.5),
            "p95": quantile(slowest_scope_ms, 0.95),
        },
    }
    return result, per_session


# --------------------------------------------------------------------------- #
# Transcripts
# --------------------------------------------------------------------------- #


def host_session_id(session_id: str) -> str:
    """``claude_<uuid>`` -> ``<uuid>`` (the transcript file stem)."""
    return session_id.split("_", 1)[1] if "_" in session_id else session_id


def find_transcript(claude_dir: Path, host_id: str) -> Optional[Path]:
    if not re.match(r"^[0-9a-fA-F-]{8,64}$", host_id):
        return None
    matches = list((claude_dir / "projects").glob("*/%s.jsonl" % host_id))
    return max(matches, key=lambda p: p.stat().st_size) if matches else None


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text") or ""))
            elif kind == "tool_use":
                parts.append(json.dumps(block.get("input") or {}, ensure_ascii=False, default=str))
            elif kind == "tool_result":
                parts.append(_block_text(block.get("content")))
    return "\n".join(parts)


def _is_cognee_hook(attachment: Dict[str, Any]) -> bool:
    blob = " ".join(
        str(attachment.get(k) or "")[:2000] for k in ("command", "hookName", "stderr", "stdout")
    ).lower()
    return "cognee" in blob or any(message in blob for message in _HOOK_STATUS_MESSAGES)


def _hook_script(text: str) -> str:
    """The plugin script a hook command, status message or error refers to ('' if not ours)."""
    lowered = text.lower()
    for message, script in _STATUS_TO_SCRIPT.items():
        if message in lowered:
            return script
    match = _SCRIPT_NAME_RE.search(text)
    if match and (match.group(1) in _COGNEE_HOOK_SCRIPTS or "cognee" in lowered):
        return match.group(1)
    return ""


def _error_signature(stderr: str) -> str:
    lines = [ln.strip() for ln in str(stderr or "").splitlines() if ln.strip()]
    error_line = next(
        (ln for ln in reversed(lines) if re.match(r"^[A-Za-z_.]*(Error|Exception)\b", ln)), ""
    )
    script = re.search(r"scripts/([\w.\-]+\.(?:py|sh))", str(stderr or ""))
    text = error_line or (lines[-1] if lines else "unknown error")
    return redact(("%s: " % script.group(1) if script else "") + text, 160)


def analyze_transcript(
    path: Path, since: Optional[datetime], until: Optional[datetime], recalls: List[Dict[str, Any]]
) -> Dict[str, Any]:
    usage_by_message: Dict[str, Dict[str, int]] = {}
    anonymous_usage: List[Dict[str, int]] = []
    hook_runs: List[Dict[str, Any]] = []
    stop_summaries = 0
    skills: Counter = Counter()
    injected_chars = 0
    pieces: List[str] = []
    offsets: List[Tuple[datetime, int]] = []
    compactions: List[Tuple[datetime, int]] = []
    length = 0

    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return {}
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            ts = parse_ts(record.get("timestamp"))
            in_window = (
                ts is not None and not (since and ts < since) and not (until and ts >= until)
            )
            kind = record.get("type")
            message = record.get("message") if isinstance(record.get("message"), dict) else {}

            if kind == "system" and "compact" in str(record.get("subtype") or "").lower() and ts:
                compactions.append((ts, length))
            if record.get("isCompactSummary") and ts:
                compactions.append((ts, length))

            if kind in ("user", "assistant") and ts is not None and not (until and ts >= until):
                text = normalize_text(_block_text(message.get("content"))[:MAX_TRANSCRIPT_CHUNK])
                if text:
                    pieces.append(text)
                    length += len(text) + 1
                    offsets.append((ts, length))

            if not in_window:
                continue

            if kind == "system" and record.get("subtype") == "stop_hook_summary":
                # A Stop hook that succeeds leaves no attachment at all, so this per-turn
                # summary is the only place successful runs can be counted.
                stop_summaries += 1
                failures = {}
                for error in record.get("hookErrors") or []:
                    script = _hook_script(str(error))
                    if script:
                        failures[script] = str(error)
                runs = []
                for info in record.get("hookInfos") or []:
                    command = str(info.get("command") or "") if isinstance(info, dict) else ""
                    script = _hook_script(command)
                    if script:
                        runs.append((script, command, info.get("durationMs")))
                runs += [
                    (script, script, None)
                    for script in failures
                    if script not in dict((name, None) for name, _command, _ms in runs)
                ]
                for script, command, duration in runs:
                    error = failures.get(script)
                    versioned = _PLUGIN_VERSION_RE.search(error or "")
                    hook_runs.append(
                        {
                            "event": "Stop",
                            "hook": script,
                            "source": "summary",
                            "version": versioned.group(1) if versioned else "",
                            "ok": error is None,
                            "ms": float(duration) if isinstance(duration, (int, float)) else None,
                            "signature": "" if error is None else _error_signature(error),
                            "label": redact(command, 60),
                        }
                    )

            if kind == "assistant":
                usage = message.get("usage")
                if isinstance(usage, dict):
                    row = {
                        k: int(usage.get(k) or 0)
                        for k in (
                            "input_tokens",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens",
                            "output_tokens",
                        )
                    }
                    # One API response is written as several lines (one per content
                    # block) that repeat the same usage: count each message once.
                    if message.get("id"):
                        usage_by_message[str(message["id"])] = row
                    else:
                        anonymous_usage.append(row)
                for block in (
                    message.get("content") if isinstance(message.get("content"), list) else []
                ):
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    if (
                        block.get("name") == "Skill"
                        and "cognee" in str(tool_input.get("skill") or "").lower()
                    ):
                        skills[str(tool_input.get("skill"))] += 1
                    elif block.get("name") == "Bash":
                        # Running a plugin script counts; grepping or listing one does not.
                        command = str(tool_input.get("command") or "").lstrip()
                        ran = (
                            _PLUGIN_SCRIPT_RE.search(command)
                            if command.startswith(_RUNNERS)
                            else None
                        )
                        if ran:
                            skills["script:" + ran.group(1)] += 1
            elif kind == "user":
                text = _block_text(message.get("content"))
                for name in re.findall(
                    r"<command-name>/?([\w:\-]*cognee[\w:\-]*)</command-name>", text
                ):
                    skills["slash:" + name] += 1

            attachment = (
                record.get("attachment") if isinstance(record.get("attachment"), dict) else None
            )
            if attachment:
                a_type = attachment.get("type")
                if a_type in (
                    "hook_success",
                    "hook_non_blocking_error",
                    "hook_blocking_error",
                ) and _is_cognee_hook(attachment):
                    ok = a_type == "hook_success"
                    duration = attachment.get("durationMs")
                    blob = " ".join(
                        str(attachment.get(k) or "")[:4000] for k in ("stderr", "stdout", "command")
                    )
                    versioned = _PLUGIN_VERSION_RE.search(blob)
                    hook_runs.append(
                        {
                            "event": str(attachment.get("hookEvent") or "unknown"),
                            "hook": _hook_script(blob),
                            "source": "attachment",
                            "version": versioned.group(1) if versioned else "",
                            "ok": ok,
                            "ms": float(duration) if isinstance(duration, (int, float)) else None,
                            "signature": ""
                            if ok
                            else _error_signature(
                                attachment.get("stderr") or attachment.get("stdout") or ""
                            ),
                            "label": redact(attachment.get("command") or "", 60),
                        }
                    )
                elif a_type == "hook_additional_context":
                    text = _block_text(attachment.get("content"))
                    if "cognee" in text.lower():
                        injected_chars += len(text)

    if stop_summaries:
        # The error attachment repeats what the turn summary already says.
        hook_runs = [
            run
            for run in hook_runs
            if not (run["event"] == "Stop" and run["source"] == "attachment")
        ]

    corpus = " ".join(pieces)
    in_transcript_items = checked_items = in_transcript_chars = checked_chars = 0
    for recall in sorted(recalls, key=lambda r: r["ts"]):
        end = 0
        for stamp, offset in offsets:
            if stamp > recall["ts"]:
                break
            end = offset
        start = 0
        for stamp, offset in compactions:
            if stamp <= recall["ts"]:
                start = offset  # text before the last compaction is no longer in context
        if end <= start:
            continue
        for item in recall["items"]:
            probes = item_probes(item)
            if not probes:
                continue
            checked_items += 1
            checked_chars += len(item)
            found = sum(1 for probe in probes if corpus.find(probe, start, end) != -1)
            if found >= 2:
                recall["transcript_match_chars"] = recall.get("transcript_match_chars", 0) + len(
                    item
                )
                in_transcript_items += 1
                in_transcript_chars += len(item)

    totals = {
        k: 0
        for k in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
    }
    for row in list(usage_by_message.values()) + anonymous_usage:
        for key in totals:
            totals[key] += row[key]
    return {
        "usage": totals,
        "api_messages": len(usage_by_message) + len(anonymous_usage),
        "hook_runs": hook_runs,
        "skills": dict(skills),
        "injected_chars_seen": injected_chars,
        "compactions": len(compactions),
        "in_transcript": {
            "items": in_transcript_items,
            "checked_items": checked_items,
            "chars": in_transcript_chars,
            "checked_chars": checked_chars,
        },
    }


def analyze_transcripts(
    claude_dir: Path,
    plugin_dir: Path,
    since: Optional[datetime],
    until: Optional[datetime],
    per_session: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    host_ids = {host_session_id(sid): sid for sid in per_session}
    sessions_dir = plugin_dir / "sessions"
    if sessions_dir.is_dir():
        for path in sessions_dir.glob("*.json"):
            try:
                touched = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if (since and touched < since) or path.stem in host_ids:
                continue
            host_ids[path.stem] = ""
    usage = Counter()
    skills: Counter = Counter()
    hook_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"ok": 0, "failed": 0, "ms": []})
    signatures: Counter = Counter()
    per_hook: Dict[str, Dict[str, int]] = defaultdict(lambda: {"ok": 0, "failed": 0})
    found = missing = api_messages = compactions = 0
    injected_tokens_matched = 0
    fresh_input_matched = 0
    sessions_with_usage = 0
    redundancy = Counter()

    for host_id, sid in sorted(host_ids.items()):
        path = find_transcript(claude_dir, host_id)
        if path is None:
            # Launch records without a transcript are short-lived probe sessions
            # started by other tools; only a session that recalled is "missing".
            missing += 1 if sid else 0
            continue
        found += 1
        data = analyze_transcript(path, since, until, per_session.get(sid, []))
        if not data:
            continue
        usage.update(data["usage"])
        api_messages += data["api_messages"]
        compactions += data["compactions"]
        skills.update(data["skills"])
        redundancy.update(data["in_transcript"])
        for run in data["hook_runs"]:
            bucket = hook_stats[run["event"]]
            bucket["ok" if run["ok"] else "failed"] += 1
            if run["ok"] and run["ms"] is not None:
                bucket["ms"].append(run["ms"])
            name = run["hook"] or run["event"]
            per_hook[name]["ok" if run["ok"] else "failed"] += 1
            if not run["ok"]:
                key = (run["event"], run["signature"], run["label"], run["version"], name)
                signatures[key] += 1
        fresh = data["usage"]["input_tokens"] + data["usage"]["cache_creation_input_tokens"]
        session_chars = sum(r["chars"] for r in per_session.get(sid, []))
        if fresh and session_chars:
            sessions_with_usage += 1
            fresh_input_matched += fresh
            injected_tokens_matched += int(session_chars / CHARS_PER_TOKEN)

    runs = sum(b["ok"] + b["failed"] for b in hook_stats.values())
    crashed = sum(b["failed"] for b in hook_stats.values())
    return {
        "transcripts_found": found,
        "transcripts_missing": missing,
        "api_messages": api_messages,
        "compactions": compactions,
        "usage": dict(usage),
        "fresh_input_tokens": usage["input_tokens"] + usage["cache_creation_input_tokens"],
        "overhead": {
            "sessions": sessions_with_usage,
            "injected_tokens_est": injected_tokens_matched,
            "fresh_input_tokens": fresh_input_matched,
            "share": ratio(injected_tokens_matched, fresh_input_matched),
        },
        "hooks": {
            event: {
                "ok": b["ok"],
                "failed": b["failed"],
                "n_timed": len(b["ms"]),
                "p50_ms": quantile(b["ms"], 0.5),
                "p95_ms": quantile(b["ms"], 0.95),
                "max_ms": max(b["ms"]) if b["ms"] else None,
            }
            for event, b in sorted(hook_stats.items())
        },
        "hook_runs": runs,
        "hook_crashes": crashed,
        "hook_crash_rate": ratio(crashed, runs),
        "crash_signatures": [
            {
                "event": event,
                "signature": signature,
                "label": label,
                "version": version,
                "hook": hook,
                "count": count,
            }
            for (event, signature, label, version, hook), count in signatures.most_common(5)
        ],
        "per_hook": {name: dict(counts) for name, counts in sorted(per_hook.items())},
        "explicit_usage": dict(skills),
        "in_transcript": {
            "checked_items": redundancy["checked_items"],
            "items": redundancy["items"],
            "item_share": ratio(redundancy["items"], redundancy["checked_items"]),
            "char_share": ratio(redundancy["chars"], redundancy["checked_chars"]),
            "tokens_est": int(redundancy["chars"] / CHARS_PER_TOKEN),
        },
    }


# --------------------------------------------------------------------------- #
# Server (optional)
# --------------------------------------------------------------------------- #


def analyze_server(
    base_url: str,
    api_key: str,
    since: Optional[datetime],
    until: Optional[datetime],
    opener: Callable[..., Any] = urllib.request.urlopen,
    max_pages: int = 10,
) -> Dict[str, Any]:
    """What landed server-side: operations and pipeline runs in the window. Fail-soft."""
    if not base_url:
        return {
            "available": False,
            "reason": "no server URL (pass --server-url or set COGNEE_BASE_URL)",
        }
    terminal: Dict[str, Dict[str, Any]] = {}
    reached_window_start = False
    try:
        for page in range(max_pages):
            url = "%s/api/v1/activity/pipeline-runs?limit=500&offset=%d" % (
                base_url.rstrip("/"),
                page * 500,
            )
            request = urllib.request.Request(url, headers={"X-Api-Key": api_key} if api_key else {})
            with opener(request, timeout=30) as response:
                rows = json.loads(response.read().decode("utf-8") or "[]")
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                created = parse_ts(row.get("created_at"))
                if created is None or (until and created >= until):
                    continue
                if since and created < since:
                    reached_window_start = True
                    continue
                key = str(row.get("pipeline_run_id") or row.get("id"))
                previous = terminal.get(key)
                is_terminal = bool(row.get("outcome")) or any(
                    word in str(row.get("status") or "")
                    for word in ("COMPLETED", "ERRORED", "ABANDONED")
                )
                if previous is None or (is_terminal and not previous["_terminal"]):
                    terminal[key] = dict(row, _terminal=is_terminal)
            if reached_window_start or len(rows) < 500:
                break
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"available": False, "reason": redact(exc, 160)}

    operations: Dict[str, Counter] = defaultdict(Counter)
    pipelines: Dict[str, Counter] = defaultdict(Counter)
    tokens_in = tokens_out = 0
    for row in terminal.values():
        if row.get("pipeline_name"):
            status = str(row.get("status") or "")
            state = (
                "completed"
                if "COMPLETED" in status
                else "errored"
                if "ERRORED" in status
                else "abandoned"
                if "ABANDONED" in status
                else "unfinished"
            )
            pipelines[str(row["pipeline_name"])][state] += 1
        else:
            outcome = str(row.get("outcome") or "unfinished")
            if row.get("background") and outcome == "succeeded":
                outcome = "accepted_in_background"
            operations[str(row.get("operation_name") or "unknown")][outcome] += 1
        if not row.get("parent_operation_id"):  # a parent already includes its children's tokens
            tokens_in += int(row.get("tokens_in") or 0)
            tokens_out += int(row.get("tokens_out") or 0)
    return {
        "available": True,
        "runs": len(terminal),
        "operations": {name: dict(c) for name, c in sorted(operations.items())},
        "pipelines": {name: dict(c) for name, c in sorted(pipelines.items())},
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "note": "Distillation is a stage inside improve; it shows up as the add/cognify runs "
        "nested under it.",
    }


# --------------------------------------------------------------------------- #
# Scorecard, grade, advice
# --------------------------------------------------------------------------- #


def log_files(plugin_dir: Path) -> List[Dict[str, Any]]:
    sizes = []
    try:
        entries = list(plugin_dir.iterdir())
    except OSError:
        return []
    for path in entries:
        if path.is_file() and (".log" in path.name) and not path.name.endswith(".lock"):
            try:
                sizes.append({"file": path.name, "mb": round(path.stat().st_size / 1_048_576, 1)})
            except OSError:
                continue
    return sorted(sizes, key=lambda item: -item["mb"])[:5]


def usage_level(
    recall: Dict[str, Any], hook: Dict[str, Any], transcripts: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    invoked = (transcripts or {}).get("explicit_usage", {})
    explicit = sum(invoked.values())
    advanced = dict(hook.get("advanced_usage") or {})
    for name, count in invoked.items():  # code graph counts only when someone asked for it
        if "cognee-code" in name or "switch-datasets" in name:
            advanced[name] = count
    passive = recall["recalls"] > 0 or hook["stores"]["attempted"] > 0
    if advanced:
        level, name = 3, "advanced"
    elif explicit:
        level, name = 2, "active"
    elif passive:
        level, name = 1, "passive"
    else:
        level, name = 0, "installed"
    return {
        "level": level,
        "name": name,
        "meaning": {
            0: "hooks are installed but nothing was recalled or stored in this window",
            1: "only the automatic hooks ran; no skill was invoked by the user or the model",
            2: "skills were invoked explicitly (search / remember / sync)",
            3: "advanced features in use (dataset switching, code graph or shared memory)",
        }[level],
        "explicit_invocations": (transcripts or {}).get("explicit_usage", {}),
        "explicit_measurable": transcripts is not None
        and transcripts.get("transcripts_found", 0) > 0,
        "advanced_evidence": advanced,
        "recalls_per_session": ratio(recall["recalls"], recall["sessions"]),
    }


def metric_values(card: Dict[str, Any]) -> Dict[str, Tuple[Optional[float], int]]:
    hook, recall, transcripts = card["hook_log"], card["recall"], card.get("transcripts") or {}
    lookup_ms = hook["recall_events"]["lookup_ms"]
    if len(lookup_ms) >= 10:
        recall_p95, recall_n = quantile(lookup_ms, 0.95), len(lookup_ms)
    else:  # slowest scope is a lower bound on the hook's wall clock
        recall_p95, recall_n = recall["slowest_scope_ms"]["p95"], recall["slowest_scope_ms"]["n"]
    start = (transcripts.get("hooks") or {}).get("SessionStart") or {}
    files = card["log_health"]["largest_files"]
    recalls_n = recall["recalls"] or hook["recall_events"]["lookups_logged"]
    return {
        "store_landed_rate": (hook["stores"]["landed_rate"], hook["stores"]["attempted"]),
        "improve_ok_rate": (hook["improve"]["ok_rate"], hook["improve"]["runs"]),
        "recall_error_rate": (
            ratio(min(recalls_n, hook["recall_events"]["recalls_with_error"]), recalls_n),
            recalls_n,
        ),
        "hook_crash_rate": (transcripts.get("hook_crash_rate"), transcripts.get("hook_runs", 0)),
        "hit_rate": (recall["hit_rate"], recall["recalls"]),
        "novelty_rate": (recall["novelty_rate"], recall["recalls"]),
        "graph_hit_share": (recall["graph_hit_share"], recall["total_hits"]),
        "median_injected_tokens": (recall["median_injected_tokens"], recall["recalls"]),
        "overhead_share": (
            (transcripts.get("overhead") or {}).get("share"),
            (transcripts.get("overhead") or {}).get("sessions", 0),
        ),
        "empty_injection_rate": (recall["empty_injection_rate"], recall["recalls"]),
        "recall_p95_ms": (recall_p95, recall_n),
        "session_start_p95_ms": (start.get("p95_ms"), start.get("n_timed", 0)),
        "top_event_share": (hook["top_event_share"], hook["lines_in_window"]),
        "largest_log_mb": ((files[0]["mb"] if files else None), 1 if files else 0),
    }


def score_metric(
    value: float, direction: str, target: float, gate: float, floor: float
) -> Tuple[float, bool]:
    span = (target - floor) if direction == "higher" else (floor - target)
    raw = ((value - floor) if direction == "higher" else (floor - value)) / span if span else 1.0
    passed = value >= gate if direction == "higher" else value <= gate
    return round(max(0.0, min(1.0, raw)) * 100, 1), passed


def compute_grade(values: Dict[str, Tuple[Optional[float], int]]) -> Dict[str, Any]:
    metrics = []
    by_sub: Dict[str, List[float]] = defaultdict(list)
    failed_gates = []
    for key, sub, label, direction, target, gate, floor, min_n, unit in METRIC_SPECS:
        value, n = values.get(key, (None, 0))
        entry = {
            "key": key,
            "subscore": sub,
            "label": label,
            "value": value,
            "n": int(n or 0),
            "unit": unit,
            "direction": direction,
            "target": target,
            "gate": gate,
            "min_n": min_n,
        }
        if value is None:
            entry.update(status="no_data", score=None)
        elif (n or 0) < min_n:
            entry.update(status="low_n", score=None)
        else:
            score, passed = score_metric(float(value), direction, target, gate, floor)
            entry.update(status="pass" if passed else "fail", score=score)
            by_sub[sub].append(score)
            if not passed:
                failed_gates.append(key)
        metrics.append(entry)
    subscores = {
        sub: (round(sum(by_sub[sub]) / len(by_sub[sub]), 1) if by_sub[sub] else None)
        for sub in SUBSCORES
    }
    scored = [v for v in subscores.values() if v is not None]
    overall: Dict[str, Any] = {"score": None, "letter": None, "failed_gates": failed_gates}
    if len(scored) < MIN_SUBSCORES_FOR_GRADE:
        overall["status"] = "insufficient_data"
    else:
        overall["score"] = round(sum(scored) / len(scored), 1)
        if failed_gates:
            overall["status"] = "gates_failed"
        else:
            overall["status"] = "graded"
            overall["letter"] = next(
                letter for bound, letter in LETTERS if overall["score"] >= bound
            )
    return {
        "metrics": metrics,
        "subscores": subscores,
        "overall": overall,
        "not_measured": [
            "friction (needs a scripted fresh-install run: steps, failures, time to first recall)"
        ],
    }


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else "%.0f%%" % (value * 100)


def build_advice(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Evidence-backed recommendations.

    ``pain`` = broken today, ``feature`` = worth building, ``ops`` = housekeeping.
    """
    advice: List[Dict[str, Any]] = []
    hook, recall = card["hook_log"], card["recall"]
    transcripts = card.get("transcripts") or {}

    def add(identifier, kind, priority, title, evidence, why, action, impact):
        advice.append(
            {
                "id": identifier,
                "kind": kind,
                "priority": priority,
                "title": title,
                "evidence": evidence,
                "why": why,
                "action": action,
                "impact": round(float(impact), 3),
            }
        )

    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for crash in transcripts.get("crash_signatures", []):
        entry = merged.setdefault(
            (crash["event"], crash["signature"]), dict(crash, count=0, versions=[])
        )
        entry["count"] += crash["count"]
        if crash.get("version") and crash["version"] not in entry["versions"]:
            entry["versions"].append(crash["version"])
    for crash in merged.values():
        # Rate per hook, not per event: three hooks run on Stop and only one may be broken.
        bucket = (transcripts.get("per_hook") or {}).get(crash.get("hook") or "") or (
            transcripts.get("hooks") or {}
        ).get(crash["event"], {})
        runs = bucket.get("ok", 0) + bucket.get("failed", 0)
        rate = ratio(crash["count"], runs) or 0.0
        pep604 = "unsupported operand type(s) for |" in crash["signature"]
        clears = (
            "clearing claude context" in crash["label"].lower()
            or "clear-transcript" in crash["signature"]
        )
        why = (
            "The memory block injected on each prompt is never stripped from the transcript, so "
            "every recall "
            "stays in the context window and repeats accumulate."
            if clears
            else "A crashing hook silently drops its work for every affected turn."
        )
        action = (
            "Python older than 3.10 evaluates `X | None` annotations when the function is defined. "
            "Add "
            "`from __future__ import annotations` (or use typing.Optional) and run the hooks under "
            "the oldest "
            "supported python3 in CI. Newer plugin builds already carry this fix: update the "
            "plugin."
            if pep604
            else "Reproduce with the logged stderr, fix, and add a test that runs the script under "
            "the oldest supported python3."
        )
        installed = str(card["install"].get("version") or "")
        seen_under = ", ".join("v" + v for v in sorted(crash["versions"]))
        superseded = (
            bool(crash["versions"]) and bool(installed) and installed not in crash["versions"]
        )
        if superseded:
            action = (
                "Seen only under %s; v%s is installed now. Confirm the fix: `report --since "
                "last-update` "
                "should list %s hook runs with 0 crashes. %s"
                % (seen_under, installed, crash["event"], action)
            )
        add(
            "hook_crash:%s" % crash["event"],
            "pain",
            "P2" if superseded else ("P0" if rate >= 0.5 else "P1"),
            "%s hook %scrashes (%s of runs)%s"
            % (
                crash["event"],
                crash["hook"] + " "
                if crash.get("hook") and crash["hook"] != crash["event"]
                else "",
                _pct(rate),
                " under " + seen_under if seen_under else "",
            ),
            "%d of %d runs failed: %s" % (crash["count"], runs, crash["signature"]),
            why,
            action,
            10 + rate,
        )

    improve = hook["improve"]
    if improve["runs"] >= 5 and (improve["ok_rate"] or 0) < 0.80:
        errors = (
            "; ".join("%s (x%d)" % (text, n) for text, n in improve["top_errors"])
            or "no error text logged"
        )
        add(
            "improve_failing",
            "pain",
            "P0" if improve["ok_rate"] < 0.5 else "P1",
            "Improve succeeds only %s of the time" % _pct(improve["ok_rate"]),
            "%d ok / %d failed; triggers %s; %s"
            % (improve["ok"], improve["failed"], improve["triggers"], errors),
            "Improve is the only path from a session into the knowledge graph. When it fails, "
            "memory stays session-local.",
            "Group failures by error text, fix the dominant one first, and log the server's status "
            "body so a "
            "busy/no-op answer is never counted as a failure.",
            9 + (1 - improve["ok_rate"]),
        )

    sync = hook["sync"]
    if sync["wrote"] + sync["not_written"] >= 3 and sync["not_written"] >= sync["wrote"]:
        add(
            "sync_not_writing",
            "pain",
            "P1",
            "Session-end sync rarely writes",
            "%d syncs wrote, %d did not, %d failed outright"
            % (sync["wrote"], sync["not_written"], sync["failed"]),
            "The final sync is the last chance to bridge a session; if it does not write, the tail "
            "of the session is lost.",
            "Trace one not-written sync end to end (drain -> improve answer) and make the log say "
            "which step declined.",
            8,
        )

    stores = hook["stores"]
    if stores["attempted"] >= 20 and (stores["buffered_share"] or 0) >= 0.30:
        add(
            "writes_buffered",
            "pain",
            "P1",
            "%s of writes go to the warm-up buffer" % _pct(stores["buffered_share"]),
            "%d stored directly, %d buffered, %d drained later, %d drain problems"
            % (
                stores["stored_directly"],
                stores["buffered"],
                stores["drained_later"],
                stores["drain_problems"],
            ),
            "Buffered writes only reach memory if a later drain succeeds, and replay has no "
            "server-side idempotency.",
            "Find why the server is not ready when hooks fire (slow boot, stale readiness marker) "
            "and shorten that window.",
            7 + stores["buffered_share"],
        )

    if stores["skipped_as_self_reference"] >= 20 and (stores["self_skip_share"] or 0) >= 0.5:
        add(
            "trace_filter_too_broad",
            "pain",
            "P1",
            "%s of tool calls are dropped as self-referential" % _pct(stores["self_skip_share"]),
            "%d Bash calls skipped vs %d writes attempted"
            % (stores["skipped_as_self_reference"], stores["attempted"]),
            "The filter skips any Bash command containing the word 'cognee', so a project whose "
            "path contains it "
            "(every cognee repo) stores almost no traces and has nothing to distill.",
            "Match real plugin invocations (CLAUDE_PLUGIN_ROOT, /.cognee-plugin/, cognee-cli, the "
            "plugin's own scripts) "
            "instead of the substring 'cognee'.",
            8 + stores["self_skip_share"],
        )

    share = recall["reinjection"]["repeated_char_share"]
    if recall["recalls"] >= 20 and share is not None and share >= 0.30:
        add(
            "reinjection",
            "feature",
            "P1" if share >= 0.5 else "P2",
            "%s of injected memory was already injected earlier in the session" % _pct(share),
            "~%s tokens of repeats across %d recalls; %d recalls injected nothing new"
            % (
                "{:,}".format(recall["reinjection"]["repeated_tokens_est"]),
                recall["recalls"],
                recall["reinjection"]["fully_repeated_recalls"],
            ),
            "Repeats cost input tokens on every turn and crowd out new memory in a fixed injection "
            "budget.",
            "Inject deltas: keep a per-session set of injected item hashes, skip items already "
            "injected since the "
            "last compaction, and re-arm the set from the PreCompact hook.",
            6 + share,
        )

    in_transcript = transcripts.get("in_transcript") or {}
    if (
        in_transcript.get("checked_items", 0) >= 50
        and (in_transcript.get("char_share") or 0) >= 0.30
    ):
        add(
            "recall_already_in_context",
            "feature",
            "P1" if in_transcript["char_share"] >= 0.5 else "P2",
            "%s of recalled text was still in the live conversation"
            % _pct(in_transcript["char_share"]),
            "%d of %d checked items matched transcript text before the recall (~%s tokens); "
            "heuristic, lower bound"
            % (
                in_transcript["items"],
                in_transcript["checked_items"],
                "{:,}".format(in_transcript["tokens_est"]),
            ),
            "Session and trace scopes echo what the model can already see; they only add value "
            "after compaction or resume.",
            "Gate the session/trace scopes on compaction or resume, or drop hits whose text is "
            "present in the transcript.",
            6 + in_transcript["char_share"],
        )

    if recall["recalls"] >= 20 and (recall["empty_injection_rate"] or 0) >= 0.10:
        add(
            "empty_injection",
            "pain",
            "P2",
            "%s of injections carry zero hits" % _pct(recall["empty_injection_rate"]),
            "~%s tokens injected with no memory in them"
            % "{:,}".format(recall["empty_injection_tokens_est"]),
            "An injection with no hits is pure overhead and teaches the model to ignore the block.",
            "Emit no additionalContext when every scope returned zero hits (keep the one-line "
            "status for the user).",
            5,
        )

    if recall["total_hits"] >= 50 and (recall["graph_hit_share"] or 0) < 0.10:
        skipped = recall["graph_skipped_rate"]
        add(
            "graph_underused",
            "feature",
            "P1",
            "Only %s of hits come from the knowledge graph" % _pct(recall["graph_hit_share"]),
            "hits by scope %s; graph scope skipped in %s of recalls"
            % (recall["scope_hits"], _pct(skipped)),
            "The graph is the product's differentiator; today recall is mostly the session echoing "
            "itself.",
            "Find why the graph scope is skipped (not built, budget, breaker), make improve land "
            "so sessions reach the "
            "graph, then weight graph hits above session echoes.",
            7,
        )

    verdicts = hook["recall_events"]["verdicts"]
    p95 = next(
        (
            m["value"]
            for m in card["grade"]["metrics"]
            if m["key"] == "recall_p95_ms" and m["status"] in ("pass", "fail")
        ),
        None,
    )
    over_budget = hook["recall_events"]["recalls_over_budget"]
    if (p95 is not None and p95 > 3000) or verdicts.get("slow", 0) >= 5 or over_budget >= 5:
        add(
            "recall_slow",
            "pain",
            "P1" if (p95 or 0) > 6000 else "P2",
            "Recall is slow",
            "p95 %s ms; %d slow verdicts; %d recalls cut off by the budget; per-scope p95 %s"
            % (
                "n/a" if p95 is None else int(p95),
                verdicts.get("slow", 0),
                over_budget,
                {s: int(v["p95"]) for s, v in recall["scope_latency_ms"].items() if v["p95"]},
            ),
            "This hook blocks the prompt: every second here is a second the user waits before the "
            "model starts.",
            "Dispatch scopes concurrently under one deadline, and skip a scope after repeated slow "
            "verdicts.",
            6,
        )

    start = (transcripts.get("hooks") or {}).get("SessionStart") or {}
    if start.get("n_timed", 0) >= 1 and (start.get("p95_ms") or 0) >= 8000:
        add(
            "session_start_slow",
            "pain",
            "P1",
            "SessionStart takes %.0f s at p95" % (start["p95_ms"] / 1000.0),
            "n=%d, max %.0f s" % (start["n_timed"], (start.get("max_ms") or 0) / 1000.0),
            "Users wait this long before they can type in a new session.",
            "Move server bootstrap and version checks fully off the blocking path; return as soon "
            "as identity is resolved.",
            6,
        )

    overhead = transcripts.get("overhead") or {}
    if overhead.get("sessions", 0) >= 3 and (overhead.get("share") or 0) >= 0.10:
        add(
            "token_overhead",
            "feature",
            "P2",
            "Injected memory is %s of fresh model input" % _pct(overhead["share"]),
            "~%s injected vs %s fresh input tokens over %d sessions (estimate)"
            % (
                "{:,}".format(overhead["injected_tokens_est"]),
                "{:,}".format(overhead["fresh_input_tokens"]),
                overhead["sessions"],
            ),
            "This is the recurring price users pay for the plugin on every turn.",
            "Set a per-turn token budget, rank items by novelty x relevance, and cut once the "
            "budget is spent.",
            5,
        )

    if hook["lines_in_window"] >= 200 and (hook["top_event_share"] or 0) >= 0.60:
        add(
            "log_spam",
            "ops",
            "P1",
            "One event is %s of hook.log" % _pct(hook["top_event_share"]),
            "`%s`; the log only reaches back to %s"
            % (hook["top_event"], to_local_label(parse_ts(hook["first_ts"]))),
            "hook.log rotates by size, so noise evicts the events every diagnosis (including this "
            "report) depends on.",
            "Log this event once per state change instead of on every tick.",
            6 + hook["top_event_share"],
        )

    for entry in card["log_health"]["largest_files"]:
        if entry["mb"] >= 100:
            add(
                "giant_log:%s" % entry["file"],
                "ops",
                "P1",
                "%s is %s MB" % (entry["file"], "{:,.0f}".format(entry["mb"])),
                "size on disk in the plugin state directory",
                "Unbounded logs fill user disks and are never read.",
                "Put the file under the same size-capped rotation as hook.log and delete oversized "
                "rotations at SessionStart.",
                5,
            )

    if hook["connectivity_failures"] >= 10:
        add(
            "connectivity",
            "pain",
            "P2",
            "%d identity/connectivity lookups failed" % hook["connectivity_failures"],
            "top failures: %s" % hook["top_failures"][:3],
            "Failed identity lookups degrade the status line and can block agent registration.",
            "Back off after consecutive failures and surface one actionable message instead of "
            "retrying silently.",
            4,
        )

    level = card["usage"]
    if level["explicit_measurable"] and level["level"] <= 1 and recall["sessions"] >= 5:
        add(
            "skills_unused",
            "feature",
            "P3",
            "No skill was invoked in %d sessions" % recall["sessions"],
            "usage level: %s" % level["name"],
            "Search, remember and sync exist but nobody reaches for them, so the plugin's value is "
            "capped at what the hooks do alone.",
            "Have the recall block name the skill to use when memory does not answer, and measure "
            "invocations after that change.",
            3,
        )

    update = hook.get("update_check") or {}
    if (
        update.get("available")
        and update.get("latest")
        and update.get("installed") != update.get("latest")
        and str(card["install"].get("version") or "") != str(update.get("latest"))
    ):
        add(
            "update_available",
            "ops",
            "P2",
            "Plugin update available: %s -> %s" % (update.get("installed"), update.get("latest")),
            "from the plugin's own update check",
            "Several findings above may already be fixed upstream.",
            "Update, then re-run this report with `--since last-update --compare`.",
            4,
        )

    if 0 < recall["recalls"] < 20:
        add(
            "low_sample",
            "ops",
            "P3",
            "Small window: %d recalls" % recall["recalls"],
            "most metrics need 20+ observations and are marked low-n",
            "Grades on a handful of events are noise.",
            "Use the plugin for a few sessions after the update, then re-run; or widen --since.",
            1,
        )

    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    return sorted(advice, key=lambda item: (order[item["priority"]], -item["impact"]))


def build_scorecard(
    args: argparse.Namespace,
    since: Optional[datetime],
    until: Optional[datetime],
    install: Dict[str, Any],
    label: str,
    with_server: bool = True,
) -> Dict[str, Any]:
    plugin_dir, claude_dir = Path(args.plugin_dir), Path(args.claude_dir)
    hook = analyze_hook_log(plugin_dir, since, until)
    recall, per_session = analyze_recalls(plugin_dir, since, until, args.session)
    transcripts = (
        None
        if args.no_transcripts
        else analyze_transcripts(claude_dir, plugin_dir, since, until, per_session)
    )
    now = datetime.now(timezone.utc)
    card: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": to_iso(now),
        "agent": args.agent,
        "install": install,
        "window": {
            "since": to_iso(since),
            "until": to_iso(until),
            "label": label,
            "days": round(((until or now) - since).total_seconds() / 86400.0, 2) if since else None,
        },
        "hook_log": hook,
        "recall": recall,
        "transcripts": transcripts,
        "log_health": {
            "largest_files": log_files(plugin_dir),
            "top_event": hook["top_event"],
            "top_event_share": hook["top_event_share"],
            "unparseable_lines": hook["unparseable_lines"],
            "hook_log_reaches_back_to": hook["first_ts"],
            "recall_audit_reaches_back_to": recall["first_ts"],
        },
        "server": None,
    }
    if with_server and args.server:
        card["server"] = analyze_server(
            args.server_url
            or os.environ.get("COGNEE_BASE_URL")
            or os.environ.get("COGNEE_LOCAL_API_URL")
            or "",
            os.environ.get("COGNEE_API_KEY", ""),
            since,
            until,
        )
    decisions = {}
    for event_ts, raw in iter_jsonl(rotated_files(plugin_dir, "hook.log"), {}):
        if event_ts is None or (since and event_ts < since) or (until and event_ts >= until):
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("event") != "bash_capture_decision":
            continue
        detail = row.get("detail") or {}
        if not isinstance(detail, dict):
            continue
        host = str(detail.get("host_session_id") or "unknown")
        if args.session and host != host_session_id(args.session):
            continue
        outcome = detail.get("outcome")
        if outcome in {"captured", "skipped_self", "skipped_low_signal"}:
            counts = decisions.setdefault(
                host, {"captured": 0, "skipped_self": 0, "skipped_low_signal": 0}
            )
            counts[outcome] += 1
    card["bash_capture"] = decisions
    card["delivery"] = delivery_summary(per_session, plugin_dir, since, until)
    card["usage"] = usage_level(recall, hook, transcripts)
    card["grade"] = compute_grade(metric_values(card))
    card["advice"] = build_advice(card)
    hook["recall_events"]["lookup_ms"] = {
        "n": len(hook["recall_events"]["lookup_ms"]),
        "p50": quantile(hook["recall_events"]["lookup_ms"], 0.5),
        "p95": quantile(hook["recall_events"]["lookup_ms"], 0.95),
    }
    notes = []
    if (
        since
        and hook["first_ts"]
        and parse_ts(hook["first_ts"])
        and parse_ts(hook["first_ts"]) > since
    ):
        notes.append(
            "hook.log only reaches back to %s, later than the window start: reliability counts are "
            "partial." % to_local_label(parse_ts(hook["first_ts"]))
        )
    if transcripts is not None and transcripts["transcripts_missing"]:
        notes.append(
            "%d session transcript(s) were not found; token and hook-latency figures cover the "
            "rest." % transcripts["transcripts_missing"]
        )
    card["notes"] = notes
    return card


COMPARE_KEYS = [
    "hit_rate",
    "novelty_rate",
    "median_injected_tokens",
    "recall_p95_ms",
    "improve_ok_rate",
    "store_landed_rate",
    "hook_crash_rate",
    "empty_injection_rate",
]


def compare_cards(current: Dict[str, Any], baseline: Dict[str, Any]) -> Dict[str, Any]:
    def values(card):
        return {m["key"]: m for m in card["grade"]["metrics"]}

    now_values, then_values = values(current), values(baseline)
    rows = []
    for key in COMPARE_KEYS:
        now_metric, then_metric = now_values[key], then_values[key]
        delta = None
        verdict = "n/a"
        if now_metric["value"] is not None and then_metric["value"] is not None:
            delta = now_metric["value"] - then_metric["value"]
            better = delta > 0 if now_metric["direction"] == "higher" else delta < 0
            verdict = "same" if abs(delta) < 1e-9 else ("better" if better else "worse")
            if min(now_metric["n"], then_metric["n"]) < now_metric["min_n"]:
                verdict += " (low n)"
        rows.append(
            {
                "key": key,
                "label": now_metric["label"],
                "unit": now_metric["unit"],
                "before": then_metric["value"],
                "before_n": then_metric["n"],
                "after": now_metric["value"],
                "after_n": now_metric["n"],
                "delta": delta,
                "verdict": verdict,
            }
        )
    return {
        "baseline_window": baseline["window"],
        "rows": rows,
        "score_before": baseline["grade"]["overall"]["score"],
        "score_after": current["grade"]["overall"]["score"],
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def fmt_value(value: Optional[float], unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == "pct":
        return "%.1f%%" % (value * 100)
    if unit == "ms":
        return "{:,.0f} ms".format(value)
    if unit == "tokens":
        return "{:,.0f} tok".format(value)
    if unit == "mb":
        return "{:,.1f} MB".format(value)
    return "%s" % value


def grade_headline(card: Dict[str, Any]) -> str:
    overall = card["grade"]["overall"]
    if overall["status"] == "graded":
        return "GRADE %s  (%.0f/100)" % (overall["letter"], overall["score"])
    if overall["status"] == "gates_failed":
        return "NO GRADE - %d gate(s) failed (provisional score %.0f/100): %s" % (
            len(overall["failed_gates"]),
            overall["score"],
            ", ".join(overall["failed_gates"]),
        )
    return "NO GRADE - not enough data in this window"


_DELIVERY_SEGMENTS = (
    "new graph context",
    "new session context",
    "repeated",
    "already in transcript",
    "zero-hit injection",
)


def delivery_summary(per_session, plugin_dir, since, until):
    """Body-free delivery evidence; mutually exclusive recall classifications.

    A recall may contain mixed items. Precedence is zero hit, full transcript
    match, full repeat, then new graph/session context. Missing bodies without
    exact delivery counters stay unmeasured instead of pretending novelty.
    """
    segments = dict.fromkeys(_DELIVERY_SEGMENTS, 0)
    sessions = []
    for sid, rows in per_session.items():
        new_chars = sum(row["new_chars"] for row in rows)
        repeated = sum(row["repeated_chars"] for row in rows)
        graph_hits = sum(row["graph_hits"] for row in rows)
        hits = sum(row["hits"] for row in rows)
        for row in rows:
            body = row["new_chars"] + row["repeated_chars"]
            if not row["hits"]:
                category = (
                    "already in transcript" if row["entries_deduped"] else "zero-hit injection"
                )
            elif body and row.get("transcript_match_chars", 0) >= body:
                category = "already in transcript"
            elif body and not row["new_chars"]:
                category = "repeated"
            elif row["new_chars"] or row["entries_kept"]:
                category = "new graph context" if row["graph_hits"] else "new session context"
            else:
                category = "unmeasured"
            segments[category] = segments.get(category, 0) + 1
        hashes = list(dict.fromkeys(h for row in rows for h in row["hashes"]))[:3]
        # The existing receipt is last-turn only: show it only when its timestamp
        # lies within this report, never attribute a later receipt to older data.
        host = host_session_id(sid)
        if re.fullmatch(r"[A-Za-z0-9_-]+", host):
            try:
                receipt = json.loads(
                    (plugin_dir / "sessions" / host / "injection-receipt.json").read_text()
                )
                stamp = parse_ts(receipt.get("recorded_at"))
                if stamp and (not since or stamp >= since) and (not until or stamp < until):
                    hashes = (
                        list(
                            dict.fromkeys(
                                item["hash"]
                                for item in receipt.get("items", [])
                                if isinstance(item, dict)
                                and item.get("reason") == "injected"
                                and re.fullmatch(r"[0-9a-f]{20,64}", str(item.get("hash", "")))
                            )
                        )[:3]
                        or hashes
                    )
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        sessions.append(
            {
                "session": sid,
                "recalls": len(rows),
                "hits": sum(bool(row["hits"]) for row in rows),
                "novel_share": ratio(new_chars, new_chars + repeated),
                "graph_share": ratio(graph_hits, hits),
                "entries_injected": sum(row["entries_kept"] for row in rows),
                "new_tokens_est": int(new_chars / CHARS_PER_TOKEN),
                "evidence_hashes": hashes,
            }
        )
    sessions.sort(key=lambda row: (-row["hits"], -(row["novel_share"] or 0), row["session"]))
    return {
        "sessions": sessions,
        "segments": segments,
        "label": "delivery quality",
        "new_tokens_est": sum(r["new_tokens_est"] for r in sessions),
        "note": "Delivery is not demonstrated task improvement. "
        "Novelty is a text-matching estimate; "
        "mixed recalls use zero-hit, transcript-match, repeated, then new-context precedence. "
        "Zero-hit includes recalls with no injected memory. Missing evidence is unmeasured.",
    }


def score_bar(value, ascii=False):
    full, empty = ("#", "-") if ascii else ("█", "░")
    cells = 0 if value is None else max(0, min(10, int(value / 10 + 0.5)))
    return "[" + full * cells + empty * (10 - cells) + "]"


def render_text(card: Dict[str, Any], markdown: bool = False, *, full=False, ascii=False) -> str:
    if full:
        return render_full_text(card, markdown)
    legacy = render_full_text(card, markdown)
    h2 = "## " if markdown else ""
    headline = ("**%s**" if markdown else "%s") % grade_headline(card)
    prefix = legacy.split(headline, 1)[0].rstrip()
    delivery = card.get("delivery", {"sessions": [], "segments": {}, "new_tokens_est": 0})
    recall, hook = card["recall"], card["hook_log"]
    lines = [
        prefix,
        "",
        h2 + "What Cognee did for you",
        "%d sessions; %d recalls with a hit; ~%s new context tokens (observed body estimate); "
        "%d graph facts delivered (retrieval hits)."
        % (
            recall["sessions"],
            sum(r["hits"] for r in delivery["sessions"]),
            delivery["new_tokens_est"],
            sum(v for k, v in recall["scope_hits"].items() if _is_graph_scope(k)),
        ),
        "Saves landed: %d; improves landed: %d; lessons distilled: %s."
        % (
            hook["stores"]["stored_directly"] + hook["stores"]["drained_later"],
            hook["improve"]["ok"],
            (card.get("server") or {}).get("lessons_distilled", "unmeasured"),
        ),
    ]
    # Keep existing measured delivery/token/latency numbers and their precision.
    happened = legacy.split(h2 + "What happened\n", 1)[1].split("\n" + h2 + "Recommendations", 1)[0]
    lines += [
        happened.rstrip(),
        "",
        h2 + "Where it helped",
        "Delivery evidence only; a recalled or injected item is not proof "
        "that it changed an answer.",
    ]
    for row in delivery["sessions"]:
        lines.append(
            "- %s: %d recalls, %d hits; novel %s; graph %s; %d injected entries; hashes %s"
            % (
                row["session"],
                row["recalls"],
                row["hits"],
                _pct(row["novel_share"]),
                _pct(row["graph_share"]),
                row["entries_injected"],
                ", ".join(row["evidence_hashes"]) or "unmeasured",
            )
        )
    if not delivery["sessions"]:
        lines.append("No session delivery evidence in this window.")
    lines += ["", h2 + "Quality"]
    if markdown:
        lines.append("```")
    for sub in SUBSCORES:
        if sub == "latency":
            continue
        score = card["grade"]["subscores"][sub]
        lines.append(
            "%s %s %s" % (score_bar(score, ascii), "n/a" if score is None else "%.0f" % score, sub)
        )
    segments = delivery["segments"]
    total = sum(segments.values())
    # Largest remainder allocation makes the stacked bar exactly ten cells.
    sizes = [10 * count / total if total else 0 for count in segments.values()]
    cells = [int(size) for size in sizes]
    for i in sorted(range(len(cells)), key=lambda i: sizes[i] - cells[i], reverse=True)[
        : 10 - sum(cells) if total else 0
    ]:
        cells[i] += 1
    symbols = "GSRTZU"
    stack = "".join(symbols[i] * count for i, count in enumerate(cells)) if total else "-" * 10
    lines.append("[%s] delivery quality (%d recalls)" % (stack, total))
    lines.extend(
        "%s=%s: %d" % (symbols[i], name, count) for i, (name, count) in enumerate(segments.items())
    )
    if markdown:
        lines.append("```")
    lines.append(delivery.get("note", "Delivery only; outcomes unmeasured."))
    # All gates retain values, sample counts, and low-n/no-data status.
    scorecard = legacy.split(h2 + "Scorecard\n", 1)[1].split("\n" + h2 + "What happened", 1)[0]
    lines += [scorecard.rstrip()]
    if card.get("comparison"):
        lines += [
            "",
            h2 + "Before vs after",
            legacy.split(h2 + "Before vs after\n", 1)[1]
            .split("\n" + h2 + "Notes", 1)[0]
            .split("\nAggregates only", 1)[0]
            .rstrip(),
        ]
    if card.get("notes"):
        lines += ["", h2 + "Notes"] + ["- " + note for note in card["notes"]]
    lines += ["", headline]
    return "\n".join(lines) + "\n"


def render_full_text(card: Dict[str, Any], markdown: bool = False) -> str:
    h1, h2 = ("# ", "## ") if markdown else ("", "")
    lines: List[str] = []
    install, window = card["install"], card["window"]
    lines.append(
        "%sCognee plugin report - %s v%s" % (h1, card["agent"], install.get("version") or "?")
    )
    if not markdown:
        lines.append("=" * 72)
    lines.append(
        "Window   : %s -> %s  [%s]"
        % (
            to_local_label(parse_ts(window["since"])) if window["since"] else "all time",
            to_local_label(parse_ts(window["until"])) if window["until"] else "now",
            window["label"],
        )
    )
    lines.append(
        "Install  : v%s, last updated %s (sha %s)"
        % (
            install.get("version") or "?",
            to_local_label(parse_ts(install.get("last_updated"))),
            install.get("git_sha") or "?",
        )
    )
    recall, hook = card["recall"], card["hook_log"]
    lines.append(
        "Activity : %d recalls, %d sessions, %d active days, usage level %d (%s)"
        % (
            recall["recalls"],
            recall["sessions"],
            recall["active_days"],
            card["usage"]["level"],
            card["usage"]["name"],
        )
    )
    lines += ["", ("**%s**" if markdown else "%s") % grade_headline(card), ""]

    lines.append("%sScorecard" % h2)
    if markdown:
        lines += ["", "| Metric | Value | n | Gate | Status |", "|---|---|---|---|---|"]
    for sub in SUBSCORES:
        score = card["grade"]["subscores"][sub]
        title = "%s  %s" % (sub.upper(), "n/a" if score is None else "%.0f/100" % score)
        if markdown:
            lines.append("| **%s** | | | | |" % title)
        else:
            lines.append("  %s" % title)
        for metric in card["grade"]["metrics"]:
            if metric["subscore"] != sub:
                continue
            gate = "%s %s" % (
                ">=" if metric["direction"] == "higher" else "<=",
                fmt_value(metric["gate"], metric["unit"]),
            )
            status = {"pass": "PASS", "fail": "FAIL", "low_n": "low-n", "no_data": "-"}[
                metric["status"]
            ]
            if markdown:
                lines.append(
                    "| %s | %s | %d | %s | %s |"
                    % (
                        metric["label"],
                        fmt_value(metric["value"], metric["unit"]),
                        metric["n"],
                        gate,
                        status,
                    )
                )
            else:
                lines.append(
                    "    %-40s %12s  n=%-6d gate %-12s %s"
                    % (
                        metric["label"],
                        fmt_value(metric["value"], metric["unit"]),
                        metric["n"],
                        gate,
                        status,
                    )
                )
    lines.append(
        "" if markdown else "    not measured: %s" % "; ".join(card["grade"]["not_measured"])
    )
    if markdown:
        lines.append("Not measured: %s" % "; ".join(card["grade"]["not_measured"]))

    lines += ["", "%sWhat happened" % h2]
    stores, improve = hook["stores"], hook["improve"]
    bullets = [
        "Recalls: %d, %s with a hit; hits by scope %s"
        % (recall["recalls"], _pct(recall["hit_rate"]), recall["scope_hits"] or "{}"),
        "Writes: %d direct, %d buffered, %d drained later, %d errors; %d tool calls skipped as "
        "self-referential"
        % (
            stores["stored_directly"],
            stores["buffered"],
            stores["drained_later"],
            stores["errors"],
            stores["skipped_as_self_reference"],
        ),
        "Improve (session -> graph): %d ok, %d failed, %d busy; triggers %s"
        % (improve["ok"], improve["failed"], improve["busy"], improve["triggers"] or "{}"),
        "Injected: ~%s tokens total, median %s, p90 %s per recall (estimate, 4 chars/token)"
        % (
            "{:,}".format(recall["injected_tokens_est"]),
            fmt_value(recall["median_injected_tokens"], "tokens"),
            fmt_value(recall["p90_injected_tokens"], "tokens"),
        ),
        "Repeats: %s of injected text had already been injected in the session (~%s tokens)"
        % (
            _pct(recall["reinjection"]["repeated_char_share"]),
            "{:,}".format(recall["reinjection"]["repeated_tokens_est"]),
        ),
    ]
    for host, counts in card.get("bash_capture", {}).items():
        bullets.append(
            "Bash %s: %d traces captured (stored or awaiting write), %d skipped-self, "
            "%d skipped-low-signal; %d PostToolUse calls classified"
            % (
                host,
                counts["captured"],
                counts["skipped_self"],
                counts["skipped_low_signal"],
                sum(counts.values()),
            )
        )
    transcripts = card.get("transcripts")
    if transcripts:
        in_transcript = transcripts["in_transcript"]
        bullets.append(
            "Already in context: %s of recalled text matched the live transcript (%d items "
            "checked; heuristic)"
            % (_pct(in_transcript["char_share"]), in_transcript["checked_items"])
        )
        usage = transcripts["usage"]
        bullets.append(
            "Model tokens: %s fresh input, %s cache read, %s output over %d API messages"
            % (
                "{:,}".format(transcripts["fresh_input_tokens"]),
                "{:,}".format(usage.get("cache_read_input_tokens", 0)),
                "{:,}".format(usage.get("output_tokens", 0)),
                transcripts["api_messages"],
            )
        )
        for event, stats in transcripts["hooks"].items():
            bullets.append(
                "Hook %s: %d ok, %d crashed, p50 %s, p95 %s"
                % (
                    event,
                    stats["ok"],
                    stats["failed"],
                    fmt_value(stats["p50_ms"], "ms"),
                    fmt_value(stats["p95_ms"], "ms"),
                )
            )
        bullets.append("Explicit use: %s" % (transcripts["explicit_usage"] or "none"))
    else:
        bullets.append(
            "Transcripts not analysed: token totals, hook latency and explicit use unavailable"
        )
    server = card.get("server")
    if server:
        if server.get("available"):
            bullets.append(
                "Server: %d runs; operations %s; pipelines %s; %s in / %s out tokens"
                % (
                    server["runs"],
                    server["operations"],
                    server["pipelines"],
                    "{:,}".format(server["tokens_in"]),
                    "{:,}".format(server["tokens_out"]),
                )
            )
        else:
            bullets.append("Server: unavailable (%s)" % server.get("reason"))
    lines += ["- " + bullet for bullet in bullets]

    lines += ["", "%sRecommendations (evidence-backed)" % h2]
    if not card["advice"]:
        lines.append("- none: nothing crossed a threshold in this window")
    for index, item in enumerate(card["advice"], 1):
        lines.append("%d. [%s %s] %s" % (index, item["priority"], item["kind"], item["title"]))
        lines.append("   evidence: %s" % item["evidence"])
        lines.append("   why     : %s" % item["why"])
        lines.append("   do      : %s" % item["action"])

    comparison = card.get("comparison")
    if comparison:
        base = comparison["baseline_window"]
        lines += [
            "",
            "%sBefore vs after" % h2,
            "baseline %s -> %s"
            % (to_local_label(parse_ts(base["since"])), to_local_label(parse_ts(base["until"]))),
        ]
        for row in comparison["rows"]:
            lines.append(
                "- %-38s %10s -> %-10s %s"
                % (
                    row["label"],
                    fmt_value(row["before"], row["unit"]),
                    fmt_value(row["after"], row["unit"]),
                    row["verdict"],
                )
            )

    if card["notes"]:
        lines += ["", "%sNotes" % h2] + ["- " + note for note in card["notes"]]
    lines += ["", "Aggregates only: no prompt or memory text is included in this report."]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Notion export
# --------------------------------------------------------------------------- #

NOTION_API = "https://api.notion.com/v1"
NOTION_DEFAULT_VERSION = "2026-03-11"
NOTION_MAX_TEXT = 2000  # characters per rich_text object
NOTION_MAX_RICH_TEXT = 100  # rich_text objects per block
NOTION_MAX_CHILDREN = 100  # blocks per request


def parse_notion_id(value: str) -> Optional[str]:
    """A page id from a bare id, a dashed uuid, or a Notion URL (the id ends the last segment)."""
    segment = str(value or "").split("?")[0].rstrip("/").split("/")[-1].replace("-", "")
    match = re.search(r"([0-9a-fA-F]{32})$", segment)
    if not match:
        return None
    raw = match.group(1).lower()
    return "%s-%s-%s-%s-%s" % (raw[:8], raw[8:12], raw[12:16], raw[16:20], raw[20:])


def _rich_text(text: str) -> List[Dict[str, Any]]:
    text = text or " "
    chunks = [text[i : i + NOTION_MAX_TEXT] for i in range(0, len(text), NOTION_MAX_TEXT)][
        :NOTION_MAX_RICH_TEXT
    ]
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks]


def _block(kind: str, text: str = "", **extra: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {} if kind == "divider" else {"rich_text": _rich_text(text)}
    body.update(extra)
    return {"object": "block", "type": kind, kind: body}


def notion_blocks(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    overall = card["grade"]["overall"]
    emoji = {"graded": "✅", "gates_failed": "⚠️", "insufficient_data": "ℹ️"}[overall["status"]]
    text_report = render_full_text(card)
    scorecard = text_report.split("Scorecard\n", 1)[-1].split("\nWhat happened", 1)[0].rstrip()
    happened = text_report.split("What happened\n", 1)[-1].split("\nRecommendations", 1)[0]
    blocks = [
        _block("callout", grade_headline(card), icon={"emoji": emoji}),
        _block(
            "paragraph", text_report.split("\n")[2] + "  |  " + text_report.split("\n")[3].strip()
        ),
        _block("heading_2", "Recommendations"),
    ]
    if not card["advice"]:
        blocks.append(_block("paragraph", "Nothing crossed a threshold in this window."))
    for item in card["advice"]:
        blocks.append(
            _block("heading_3", "[%s %s] %s" % (item["priority"], item["kind"], item["title"]))
        )
        blocks.append(_block("bulleted_list_item", "Evidence: " + item["evidence"]))
        blocks.append(_block("bulleted_list_item", "Why it matters: " + item["why"]))
        blocks.append(_block("bulleted_list_item", "Do: " + item["action"]))
    blocks += [
        _block("heading_2", "Scorecard"),
        _block("code", scorecard, language="plain text"),
        _block("heading_2", "What happened"),
    ]
    blocks += [
        _block("bulleted_list_item", line[2:])
        for line in happened.splitlines()
        if line.startswith("- ")
    ]
    if card.get("comparison"):
        compare = text_report.split("Before vs after\n", 1)[-1].split("\n\n", 1)[0]
        blocks += [
            _block("heading_2", "Before vs after"),
            _block("code", compare, language="plain text"),
        ]
    if card["notes"]:
        blocks.append(_block("heading_2", "Notes"))
        blocks += [_block("bulleted_list_item", note) for note in card["notes"]]
    blocks += [
        _block("divider"),
        _block(
            "paragraph",
            "Aggregates only: no prompt or memory text is included. "
            "Generated %s by plugin_eval.py (schema %d)." % (card["generated_at"], card["schema"]),
        ),
    ]
    return blocks


def notion_title(card: Dict[str, Any]) -> str:
    window = card["window"]
    since = to_local_label(parse_ts(window["since"]))[:10] if window["since"] else "all time"
    return "Cognee plugin report - %s v%s - %s" % (
        card["agent"],
        card["install"].get("version") or "?",
        since,
    )


class NotionError(RuntimeError):
    pass


def _notion_call(
    method: str,
    path: str,
    token: str,
    body: Dict[str, Any],
    version: str,
    opener: Callable[..., Any],
    sleep: Callable[[float], None],
) -> Dict[str, Any]:
    request = urllib.request.Request(
        NOTION_API + path,
        data=json.dumps(body).encode("utf-8"),
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Notion-Version": version,
            "Content-Type": "application/json",
        },
    )
    for attempt in range(4):
        try:
            with opener(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                try:
                    wait = float(exc.headers.get("Retry-After") or 1.0)
                except (TypeError, ValueError):
                    wait = 1.0
                sleep(min(10.0, max(0.5, wait)))
                continue
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message", "")
            except Exception:
                detail = ""
            hint = {
                401: " (check NOTION_TOKEN)",
                404: " (share the parent page with your integration: page menu -> Connections)",
            }.get(exc.code, "")
            raise NotionError(
                "Notion answered HTTP %s%s: %s" % (exc.code, hint, redact(detail, 300))
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise NotionError("could not reach Notion: %s" % redact(exc, 200)) from exc
    raise NotionError("Notion kept rate-limiting the request")


def export_to_notion(
    card: Dict[str, Any],
    token: str,
    parent: str,
    *,
    data_source: bool = False,
    title_property: str = "Name",
    version: Optional[str] = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    parent_id = parse_notion_id(parent)
    if not parent_id:
        raise NotionError(
            "could not find a page id in %r; pass the page URL or its 32-character id"
            % redact(parent, 80)
        )
    if not token:
        raise NotionError(
            "NOTION_TOKEN is not set (create an internal integration and share the parent page "
            "with it)"
        )
    version = version or os.environ.get("NOTION_VERSION") or NOTION_DEFAULT_VERSION
    blocks = notion_blocks(card)
    title = [{"type": "text", "text": {"content": notion_title(card)}}]
    body = {
        "parent": {"data_source_id": parent_id} if data_source else {"page_id": parent_id},
        "properties": {(title_property if data_source else "title"): {"title": title}},
        "children": blocks[:NOTION_MAX_CHILDREN],
    }
    page = _notion_call("POST", "/pages", token, body, version, opener, sleep)
    for start in range(NOTION_MAX_CHILDREN, len(blocks), NOTION_MAX_CHILDREN):
        _notion_call(
            "PATCH",
            "/blocks/%s/children" % page.get("id"),
            token,
            {"children": blocks[start : start + NOTION_MAX_CHILDREN]},
            version,
            opener,
            sleep,
        )
    return {"id": page.get("id"), "url": page.get("url"), "blocks": len(blocks)}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def compute_markers(
    args: argparse.Namespace, install: Dict[str, Any], now: datetime
) -> Dict[str, Any]:
    """Presets for a date picker, each with how much data it would cover."""
    plugin_dir = Path(args.plugin_dir)
    stamps: List[Tuple[datetime, str]] = []
    for ts, line in iter_jsonl(rotated_files(plugin_dir, "recall-audit.log"), {}):
        if ts is not None:
            match = re.search(r'"session_id"\s*:\s*"([^"]*)"', line, 0)
            stamps.append((ts, match.group(1) if match else ""))
    presets = []
    candidates = [
        ("last-update", "Since last plugin update"),
        ("24h", "Last 24 hours"),
        ("7d", "Last 7 days"),
        ("30d", "Last 30 days"),
        ("all", "All time"),
    ]
    for value, label in candidates:
        try:
            since = parse_when(value, now, install)
        except WhenError:
            continue
        rows = [(ts, sid) for ts, sid in stamps if since is None or ts >= since]
        presets.append(
            {
                "value": value,
                "label": label,
                "since": to_iso(since),
                "since_local": to_local_label(since) if since else "all time",
                "recalls": len(rows),
                "sessions": len({sid for _ts, sid in rows}),
            }
        )
    return {
        "install": install,
        "now": to_iso(now),
        "recall_audit": {
            "first": to_iso(min(s[0] for s in stamps)) if stamps else None,
            "last": to_iso(max(s[0] for s in stamps)) if stamps else None,
            "recalls": len(stamps),
        },
        "presets": presets,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plugin_eval.py", description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--agent",
        default="claude-code",
        help="plugin state dir under ~/.cognee-plugin (default: claude-code)",
    )
    common.add_argument("--plugin-dir", help="override the plugin state directory")
    common.add_argument(
        "--claude-dir",
        default=str(Path.home() / ".claude"),
        help="Claude Code config dir (transcripts, plugin registry)",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("outcomes", help="run or report controlled matched task trials")

    report = sub.add_parser(
        "report", parents=[common], help="scorecard, grade and recommendations for a time window"
    )
    report.add_argument(
        "--since",
        default="all",
        help="YYYY-MM-DD, ISO datetime, 90m/24h/7d/2w, today, yesterday, last-update, "
        "installed, all",
    )
    report.add_argument(
        "--until",
        default=None,
        help="same forms as --since; a bare date means the end of that day (default: now)",
    )
    report.add_argument(
        "--compare",
        action="store_true",
        help="also score the equally long window before --since and show the change",
    )
    report.add_argument("--session", help="restrict recall analysis to one session id")
    report.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    report.add_argument(
        "--full",
        action="store_true",
        help="show the original detailed report and all recommendations",
    )
    report.add_argument("--ascii", action="store_true", help="use # and - for quality bars")
    report.add_argument("--out", help="write the report to this file as well")
    report.add_argument(
        "--no-transcripts",
        action="store_true",
        help="skip Claude Code transcripts (faster; loses tokens, hook latency, explicit use)",
    )
    report.add_argument(
        "--server",
        action="store_true",
        help="also ask the Cognee server what landed (COGNEE_API_KEY, --server-url)",
    )
    report.add_argument(
        "--server-url", help="Cognee base URL (default: COGNEE_BASE_URL / COGNEE_LOCAL_API_URL)"
    )
    report.add_argument(
        "--export-notion",
        action="store_true",
        help="create a Notion page with this report (needs NOTION_TOKEN)",
    )
    report.add_argument(
        "--notion-parent",
        default=os.environ.get("NOTION_PARENT_PAGE_ID", ""),
        help="parent page URL or id (default: NOTION_PARENT_PAGE_ID)",
    )
    report.add_argument(
        "--notion-data-source",
        action="store_true",
        help="treat --notion-parent as a data source (database) id",
    )
    report.add_argument(
        "--notion-title-property",
        default="Name",
        help="title property name when the parent is a data source",
    )
    report.add_argument(
        "--notion-dry-run",
        metavar="FILE",
        help="write the Notion payload to FILE instead of sending it",
    )

    sub.add_parser(
        "markers",
        parents=[common],
        help="date-picker presets with the data each would cover (JSON)",
    )
    return parser


def _utf8_stdout() -> bool:
    """Make stdout UTF-8 where possible; report False if it cannot carry the bars.

    Windows pipes default to a legacy code page (cp1252 on Python 3.9), which cannot
    encode the quality-bar glyphs and would crash the whole report.
    """
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    if encoding == "utf8":
        return True
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        return True
    except (AttributeError, ValueError, OSError):
        return False


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "outcomes":
        from outcome_eval import main as outcome_main

        return outcome_main(argv[1:])
    if not argv or argv[0] not in ("report", "markers", "-h", "--help"):
        argv.insert(0, "report")
    args = build_parser().parse_args(argv)
    if not args.plugin_dir:
        args.plugin_dir = str(Path.home() / ".cognee-plugin" / args.agent)
    now = datetime.now(timezone.utc)
    install = load_install_info(Path(args.claude_dir))

    if args.command == "markers":
        print(json.dumps(compute_markers(args, install, now), indent=2))
        return 0

    try:
        since = parse_when(args.since, now, install)
        until = parse_when(args.until, now, install, end_of_day=True)
    except WhenError as exc:
        print("plugin_eval: %s" % exc, file=sys.stderr)
        return 2
    if since and until and since >= until:
        print("plugin_eval: --since must be earlier than --until", file=sys.stderr)
        return 2
    if not Path(args.plugin_dir).is_dir():
        print(
            "plugin_eval: no plugin state at %s (has the plugin run on this machine?)"
            % redact(args.plugin_dir),
            file=sys.stderr,
        )
        return 3

    card = build_scorecard(args, since, until, install, args.since)
    if args.compare:
        if since is None:
            print("plugin_eval: --compare needs a --since date to compare against", file=sys.stderr)
            return 2
        span = (until or now) - since
        baseline = build_scorecard(
            args, since - span, since, install, "baseline", with_server=False
        )
        card["comparison"] = compare_cards(card, baseline)

    output = (
        json.dumps(card, indent=2, default=str)
        if args.format == "json"
        else render_text(
            card,
            args.format == "markdown",
            full=args.full,
            ascii=args.ascii or not _utf8_stdout(),
        )
    )
    sys.stdout.write(output if output.endswith("\n") else output + "\n")
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")

    if args.notion_dry_run:
        payload = {
            "title": notion_title(card),
            "parent": parse_notion_id(args.notion_parent),
            "children": notion_blocks(card),
        }
        Path(args.notion_dry_run).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(
            "plugin_eval: Notion payload written to %s (nothing was sent)"
            % redact(args.notion_dry_run),
            file=sys.stderr,
        )
    elif args.export_notion:
        try:
            page = export_to_notion(
                card,
                os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY") or "",
                args.notion_parent,
                data_source=args.notion_data_source,
                title_property=args.notion_title_property,
            )
        except NotionError as exc:
            print("plugin_eval: Notion export failed: %s" % exc, file=sys.stderr)
            return 4
        print(
            "plugin_eval: exported to Notion: %s (%d blocks)"
            % (page.get("url") or page.get("id"), page["blocks"]),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
