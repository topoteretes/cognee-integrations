#!/usr/bin/env python3
"""``cognee-plugin trace``: one session's hook events and server runs, in time order.

Two sources, read as they are and written nowhere:

* the plugin's own event log (``hook.log`` and its rotated ``hook.log.1``).
  Lines carry no session id of their own, but every hook process logs the
  session key it resolved (``*_session_key``) or a session id in a detail, so a
  process's lines belong to the session when one of them names it. A process
  is a run of lines with one pid and no gap over ``_PID_RUN_GAP_SECONDS``.
* the server's ``GET /api/v1/activity/pipeline-runs`` rows for the Cognee
  session (and for the session's dataset, from the session's start on). An
  improve operation row that carries ``stages`` prints one line per stage.

Stdlib only; the server part is skipped with one line when it cannot be read.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

_PID_RUN_GAP_SECONDS = 600.0
_PAGE = 500
FOLLOW_INTERVAL_SECONDS = 2.0


def parse_time(stamp) -> float | None:
    """Epoch seconds of an ISO stamp; one without an offset is UTC."""
    try:
        parsed = datetime.fromisoformat(str(stamp or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def find_session(plugin_dir: Path, wanted: str, host_key: str = "") -> dict:
    """The launch record of ``wanted`` (a host session id or a Cognee session id),
    else of ``host_key``, else the newest one; {} when there is none."""
    records = []
    for path in (plugin_dir / "sessions").glob("*.json"):
        record = _json_file(path)
        if record:
            record.setdefault("host_key", path.stem)
            record["_mtime"] = path.stat().st_mtime
            records.append(record)
    if wanted:
        for record in records:
            ids = [record.get("host_key"), record.get("session_id")] + [
                t.get("session_id") if isinstance(t, dict) else t
                for t in record.get("touched") or []
            ]
            if wanted in ids:
                return record
        # Not a launch of this machine: still a session id the server may know.
        return {"host_key": "", "session_id": wanted}
    for record in records:
        if host_key and record.get("host_key") == host_key:
            return record
    return max(records, key=lambda r: r["_mtime"]) if records else {}


def _session_ids(record: dict) -> list:
    ids = [str(record.get("session_id") or "")]
    for item in record.get("touched") or []:
        ids.append(str(item.get("session_id") if isinstance(item, dict) else item or ""))
    return [i for i in dict.fromkeys(ids) if i]


def hook_events(lines: list, needles: list) -> list:
    """``(time, text)`` for the hook.log lines of the processes that name the session."""
    parsed = []
    for raw in lines:
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        at = parse_time(line.get("ts")) if isinstance(line, dict) else None
        if at is not None:
            parsed.append((at, line, raw))
    runs: dict = {}
    events = []
    for at, line, raw in parsed:
        pid = line.get("pid")
        run = runs.get(pid)
        if run is None or at - run["last"] > _PID_RUN_GAP_SECONDS:
            run = runs[pid] = {"lines": [], "match": False}
            events.append(run)
        run["last"] = at
        run["lines"].append((at, line))
        if any('"%s"' % needle in raw for needle in needles):
            run["match"] = True
    out = []
    for run in events:
        if not run["match"]:
            continue
        for at, line in run["lines"]:
            detail = line.get("detail")
            text = "hook   %s" % line.get("event")
            if detail:
                text += " " + json.dumps(detail, ensure_ascii=False, default=str)[:300]
            out.append((at, text))
    return out


def run_events(rows: list) -> list:
    """``(time, text)`` per server row, plus one per improve stage."""
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        at = parse_time(row.get("created_at") or row.get("started_at"))
        if at is None:
            continue
        name = row.get("operation_name") or row.get("pipeline_name") or "?"
        state = row.get("outcome") or row.get("status") or "recorded"
        text = "server %s %s: %s" % (row.get("kind") or "run", name, state)
        if row.get("background"):
            text += " (background)"
        if row.get("error_class"):
            text += " error=%s" % row["error_class"]
        if row.get("dataset_name"):
            text += " dataset=%s" % row["dataset_name"]
        out.append((at, text))
        if row.get("kind") == "operation" and name == "improve":
            for stage in row.get("stages") or []:
                if not isinstance(stage, dict):
                    continue
                line = "server   improve stage %s: %s" % (stage.get("stage"), stage.get("status"))
                if stage.get("reason"):
                    line += " (%s)" % stage["reason"]
                if stage.get("error"):
                    line += " error=%s" % str(stage["error"])[:160]
                if stage.get("duration_ms") is not None:
                    line += " %sms" % stage["duration_ms"]
                out.append((at, line))
    return out


def _fetch_runs(rt: dict, query: dict, request) -> tuple:
    status, payload = request(
        rt,
        "GET",
        "/api/v1/activity/pipeline-runs?" + urllib.parse.urlencode({**query, "limit": _PAGE}),
        timeout=15.0,
    )
    if status == 200 and isinstance(payload, list):
        return payload, ""
    if status == 0:
        return [], str(payload.get("error") or payload.get("kind"))
    return [], "HTTP %s" % status


def server_rows(rt: dict, record: dict, request) -> tuple:
    """(rows of the session and of its dataset since the session began, note)."""
    rows, seen, notes = [], set(), []
    queries = [{"session_id": sid} for sid in _session_ids(record)]
    if rt.get("dataset_id") and record.get("host_key") and record["host_key"] == rt.get("host_key"):
        queries.append({"dataset_id": rt["dataset_id"]})
    created = record.get("created_at")
    try:
        started = (parse_time(created) if isinstance(created, str) else float(created or 0)) or 0.0
    except (TypeError, ValueError):
        started = 0.0
    for query in queries:
        batch, note = _fetch_runs(rt, query, request)
        if note:
            notes.append(note)
        for row in batch:
            key = row.get("id") if isinstance(row, dict) else None
            at = parse_time((row or {}).get("created_at")) if isinstance(row, dict) else None
            if key is None or key in seen:
                continue
            if "dataset_id" in query and at is not None and at < started:
                continue
            seen.add(key)
            rows.append(row)
    return rows, "; ".join(dict.fromkeys(notes))


def _log_lines(plugin_dir: Path) -> list:
    lines = []
    for name in ("hook.log.1", "hook.log"):
        try:
            lines.extend((plugin_dir / name).read_text(encoding="utf-8").splitlines())
        except OSError:
            pass
    return lines


def main(plugin_dir: Path, session: str, follow: bool, *, out=print, sleep=time.sleep) -> int:
    import _code_analysis

    rt = _code_analysis.runtime()
    record = find_session(plugin_dir, session, rt.get("host_key") or "")
    if not record:
        out("cognee-plugin trace: no session found under %s" % (plugin_dir / "sessions"))
        return 1
    needles = [n for n in [str(record.get("host_key") or "")] + _session_ids(record) if n]
    out(
        "# session %s (Cognee session %s, dataset %s), server %s"
        % (
            record.get("host_key") or "-",
            record.get("session_id") or "-",
            record.get("dataset") or "-",
            rt["service_url"],
        )
    )
    printed = set()
    noted = ""
    while True:
        rows, note = server_rows(rt, record, _code_analysis.request)
        if note and note != noted:
            out("# server runs not read: %s" % note)
            noted = note
        events = hook_events(_log_lines(plugin_dir), needles) + run_events(rows)
        for at, text in sorted(events, key=lambda event: event[0]):
            if (at, text) not in printed:
                printed.add((at, text))
                out("%s %s" % (_stamp(at), text))
        if not follow:
            return 0
        try:
            sleep(FOLLOW_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            return 0
