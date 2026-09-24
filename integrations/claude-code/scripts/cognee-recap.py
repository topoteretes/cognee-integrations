#!/usr/bin/env python3
"""Time-windowed recaps of what the coding agents did — standup, digest, timeline.

Everything comes from the running Cognee server's existing endpoints; nothing
here is new server surface:

* ``GET /api/v1/sessions?range=…`` — the sessions active in the window
  (``started_at`` / ``last_activity_at``);
* ``GET /api/v1/sessions/{id}`` — each session's recent prompts and answers
  (``qas``: ``time``, ``question``, ``answer``, ``context.cwd``) and tool
  calls (``traces``: ``origin_function``, ``status``, ``method_params``);
* ``POST /api/v1/recall`` (graph scope, context only — no LLM call) — the
  knowledge graph's passages about a topic, each stamped with the session and
  date it was learned in.

The output is a deterministic Markdown skeleton; the *summarising* is left to
the model running the skill (``cognee-standup`` / ``cognee-digest`` /
``cognee-timeline``), which is what has the judgement to say "finished X,
blocked on Y". ``--json`` returns the same data for other consumers.

Usage::

    cognee-recap.py standup  [--since 24h] [--projects <substr>,…]
    cognee-recap.py digest   [--since 7d]
    cognee-recap.py timeline <topic> [--since 30d]
    common: [--json] [--all-sessions] [--max-sessions N] [--session-key <host id>]

Sessions default to the coding-agent ones (``claude_``/``codex_``/
``antigravity_``/``agy_`` ids); ``--all-sessions`` includes every session the
identity can see (MCP, maintenance jobs, …). Stdlib only; server-first with no
CLI fallback — a recap that quietly answered from a different backend would
be worse than none.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (  # noqa: E402
    _json_http_request,
    _local_api_url,
    recall_via_http,
    resolve_active_dataset,
    shell_runtime_overrides,
)

AGENT_PREFIXES = ("claude_", "codex_", "antigravity_", "agy_")
#: Tools whose ``file_path`` argument means "this file was changed".
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
DEFAULT_SINCE = {"standup": "24h", "digest": "7d", "timeline": "30d"}
DEFAULT_MAX_SESSIONS = 25
DETAIL_WORKERS = 6
PAGE = 200
MAX_PAGES = 5
PROMPT_CHARS = 140
PASSAGE_CHARS = 400
TIMELINE_TOP_K = 12
DIGEST_QUERY = "decisions, outcomes and lessons from recent work"

_LEARNING_HEADER = re.compile(
    r"^#\s*Session learning(?:\s+—\s+(?P<date>\d{4}-\d{2}-\d{2}))?"
    r"\s*\(session\s+(?P<sid>[^)\s]+)\)",
    re.M,
)
_SESSION_ID_LINE = re.compile(r"^Session ID:\s*(?P<sid>\S+)", re.M)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def parse_since(text: str, now: datetime | None = None) -> tuple[datetime, str]:
    """``24h`` / ``7d`` / ``2w`` / ``today`` / ``yesterday`` / ``week`` / ``month`` / ``all`` /
    ISO date -> (cutoff, label). Calendar words are local-time midnights."""
    now = now or datetime.now(timezone.utc)
    raw = (text or "").strip().lower()
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    words = {
        "today": (midnight, "today"),
        "yesterday": (midnight - timedelta(days=1), "since yesterday"),
        "week": (midnight - timedelta(days=midnight.weekday()), "this week (since Monday)"),
        "month": (now - timedelta(days=30), "last 30 days"),
        "all": (datetime(2000, 1, 1, tzinfo=timezone.utc), "all time"),
    }
    if raw in words:
        return words[raw]
    m = re.fullmatch(r"(\d+)\s*([hdw])", raw)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        label = f"last {n}{unit}"
        return now - delta, label
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(
            f"cannot parse --since {text!r} "
            "(use 24h, 7d, 2w, today, yesterday, week, month, all, or a date)"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed, f"since {raw}"


def range_bucket(cutoff: datetime, now: datetime | None = None) -> str:
    """The server's coarsest ``range`` that still covers the cutoff (finer filtering is ours)."""
    now = now or datetime.now(timezone.utc)
    age = now - cutoff
    if age <= timedelta(hours=24):
        return "24h"
    if age <= timedelta(days=7):
        return "7d"
    if age <= timedelta(days=30):
        return "30d"
    return "all"


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _local(dt: datetime | None, fmt: str = "%H:%M") -> str:
    return dt.astimezone().strftime(fmt) if dt else "?"


def _span(start: datetime | None, end: datetime | None) -> str:
    """``Tue 21:13 → 04:45``, or ``Tue 21:13 → Thu 04:45`` when the session crossed days."""
    same_day = start and end and start.astimezone().date() == end.astimezone().date()
    return f"{_local(start, '%a %H:%M')} → {_local(end, '%H:%M' if same_day else '%a %H:%M')}"


def _event_day(value) -> tuple[str, str]:
    """``(day heading, clock)`` for a timeline event time.

    A bare ``YYYY-MM-DD`` is a calendar day (graph learning header) and is shown
    as that day with no clock; an instant is shown in local time.
    """
    text = str(value or "")
    if not text:
        return "(undated)", "--:--"
    if len(text) == 10:
        try:
            return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d %a"), "  —  "
        except ValueError:
            return text, "  —  "
    when = parse_time(text)
    if not when:
        return "(undated)", "--:--"
    return _local(when, "%Y-%m-%d %a"), _local(when)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' if n != 1 else ''}"


def _one_line(value) -> str:
    """A prompt as one line: multi-line pastes must not break the Markdown list."""
    return " ".join(str(value or "").split())


def _duration(start: datetime | None, end: datetime | None) -> str:
    if not start or not end or end < start:
        return ""
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class Server:
    def __init__(self, session_key: str = ""):
        self.url = _local_api_url()
        rt = shell_runtime_overrides(self.url, session_key)
        self.api_key = rt.get("api_key") or None  # None -> _plugin_common resolves the default
        self.session_id = rt.get("session_id") or ""
        # Without a dataset the server searches every readable one (slow, and
        # the code graphs drown the session learnings); the launch record names
        # it, else env/default as every hook resolves it.
        self.dataset = rt.get("dataset") or resolve_active_dataset(rt.get("host_key") or "")
        self.dataset_ids = [x for x in str(rt.get("dataset_ids") or "").split(",") if x]

    def get(self, path: str, timeout: float = 15.0):
        return _json_http_request(path, None, method="GET", timeout=timeout, api_key=self.api_key)

    def sessions(self, cutoff: datetime, *, all_sessions: bool) -> list[dict]:
        """Sessions with activity after ``cutoff``, newest first."""
        bucket = range_bucket(cutoff)
        rows: list[dict] = []
        for page in range(MAX_PAGES):
            query = urllib.parse.urlencode({"range": bucket, "limit": PAGE, "offset": page * PAGE})
            body = self.get(f"/api/v1/sessions?{query}")
            chunk = body.get("sessions") if isinstance(body, dict) else None
            if not isinstance(chunk, list):
                break
            rows.extend(r for r in chunk if isinstance(r, dict))
            if not body.get("has_more"):
                break
        out = []
        for row in rows:
            sid = str(row.get("session_id") or "")
            if not all_sessions and not sid.startswith(AGENT_PREFIXES):
                continue
            last = parse_time(row.get("last_activity_at")) or parse_time(row.get("started_at"))
            if last is None or last < cutoff:
                continue
            out.append(row)
        out.sort(key=lambda r: parse_time(r.get("last_activity_at")) or cutoff, reverse=True)
        return out

    def detail(self, session_id: str) -> dict:
        try:
            body = self.get(f"/api/v1/sessions/{urllib.parse.quote(session_id, safe='')}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            raise
        return body if isinstance(body, dict) else {}

    def passages(self, topic: str, top_k: int) -> list[dict]:
        """Knowledge-graph passages about ``topic`` (context only: no LLM call)."""
        results = recall_via_http(
            topic,
            session_id=self.session_id,
            top_k=top_k,
            scope=["graph"],
            only_context=True,
            dataset=self.dataset,
            dataset_ids=self.dataset_ids or None,
            timeout=30.0,
        )
        out: list[dict] = []
        for entry in results or []:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("content") or entry.get("text") or "")
            out.extend(split_passages(text))
        return out


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


def agent_of(session_id: str) -> str:
    head = session_id.split("_", 1)[0] if "_" in session_id else session_id.split("-", 1)[0]
    return {"agy": "antigravity"}.get(head, head) or "?"


def project_of(qas: list[dict], fallback_cwd: str = "") -> str:
    """The working directory the session's prompts were issued from, else ``fallback_cwd``."""
    cwd = fallback_cwd
    for qa in qas:
        ctx = qa.get("context")
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except ValueError:
                ctx = {}
        if isinstance(ctx, dict) and ctx.get("cwd"):
            cwd = str(ctx["cwd"])
            break
    return cwd


def _git_root(path: str) -> str:
    """The nearest ancestor holding a ``.git`` (local paths only), else ""."""
    try:
        current = Path(path)
        if not current.exists():
            return ""
        if current.is_file():
            current = current.parent
        for candidate in (current, *current.parents):
            if (candidate / ".git").exists():
                return str(candidate)
    except OSError:
        pass
    return ""


def project_from_edits(edited: list[str]) -> str:
    """Where a prompt-less session worked: the repo its edited files live in.

    Sessions driven from a host that does not run the prompt hooks (a Cursor
    terminal, a scheduled job) still record tool calls, and the files those
    calls changed name the project better than the directory the host was
    launched from. The most common git root wins; a lone file's directory
    is the fallback.
    """
    roots = Counter(_git_root(p) for p in edited if p and not p.startswith("/tmp"))
    roots.pop("", None)
    if roots:
        return roots.most_common(1)[0][0]
    return ""


def _short_path(path: str, cwd: str) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1 :]
    home = str(Path.home())
    if path.startswith(home + "/"):
        return "~/" + path[len(home) + 1 :]
    return path


def summarize_session(row: dict, detail: dict, launch_cwds: dict) -> dict:
    """One session's recap record from its row + detail."""
    sid = str(row.get("session_id") or "")
    qas = [q for q in (detail.get("qas") or []) if isinstance(q, dict)]
    traces = [t for t in (detail.get("traces") or []) if isinstance(t, dict)]
    prompts = [_one_line(q.get("question")) for q in qas if _one_line(q.get("question"))]
    tools = Counter(str(t.get("origin_function") or "?") for t in traces)
    errors = sum(
        1 for t in traces if str(t.get("status") or "success").lower() not in ("success", "ok")
    )
    edited: list[str] = []
    for t in traces:
        params = t.get("method_params")
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except ValueError:
                params = {}
        if not isinstance(params, dict):
            continue
        if str(t.get("origin_function") or "") in EDIT_TOOLS:
            path = str(params.get("file_path") or params.get("path") or "").strip()
            if path and path not in edited:
                edited.append(path)
    # Project: the prompt's cwd; else the repo the edited files live in; else the
    # launch record (a host launched from the plugin cache dir names nothing useful).
    cwd = project_of(qas) or project_from_edits(edited) or launch_cwds.get(sid, "")
    started = parse_time(row.get("started_at"))
    last = parse_time(row.get("last_activity_at"))
    # The server labels a prompt-less session by its first tool name ("Shell");
    # that is not a label, so only prompt-derived ones are kept.
    label = prompts[0] if prompts else ""
    if not label and detail.get("label") and int(detail.get("msg_count") or 0) > 0:
        label = str(detail["label"])
    return {
        "session_id": sid,
        "agent": agent_of(sid),
        "project": os.path.basename(cwd.rstrip("/")) if cwd else "",
        "cwd": cwd,
        "started_at": started.isoformat() if started else None,
        "last_activity_at": last.isoformat() if last else None,
        "duration": _duration(started, last),
        "status": str(row.get("effective_status") or row.get("status") or ""),
        "label": label,
        "prompts": prompts,
        "prompt_count": int(detail.get("msg_count") or len(prompts)),
        "tool_calls": int(detail.get("tool_calls") or len(traces)),
        "tools": dict(tools.most_common(6)),
        "errors": errors,
        "files_edited": [_short_path(p, cwd) for p in edited],
        "last_answer": str(qas[-1].get("answer") or "")[:PASSAGE_CHARS] if qas else "",
        "cost_usd": row.get("cost_usd"),
    }


def launch_cwds() -> dict:
    """session_id -> cwd from the local launch records (fallback when prompts carry none)."""
    out: dict = {}
    root = Path.home() / ".cognee-plugin"
    for sub in ("claude-code", "codex", "antigravity"):
        for path in (root / sub / "sessions").glob("*.json"):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(rec, dict) and rec.get("session_id") and rec.get("cwd"):
                out[str(rec["session_id"])] = str(rec["cwd"])
    return out


def split_passages(text: str) -> list[dict]:
    """Break a graph-context blob into passages with the session/date they came from."""
    body = text
    marker = "## Relevant passages"
    if marker in body:
        body = body.split(marker, 1)[1]
    body = body.split("\n`\n", 1)[0]  # the context block is fenced in backticks
    body = body.split("\n## ", 1)[0]  # …and followed by other sections ("## Relevant entities")
    out: list[dict] = []
    for chunk in re.split(r"\n---\n", body):
        chunk = chunk.strip().strip("`").strip()
        if not chunk:
            continue
        sid, date = "", ""
        m = _LEARNING_HEADER.search(chunk)
        if m:
            sid = m.group("sid")
            date = m.group("date") or ""
            chunk = (chunk[: m.start()] + chunk[m.end() :]).strip()
        else:
            m2 = _SESSION_ID_LINE.search(chunk)
            if m2:
                sid = m2.group("sid")
                chunk = (chunk[: m2.start()] + chunk[m2.end() :]).strip()
        # Passages render as list items; a raw Q/A transcript chunk has blank
        # lines that would end the list, so every passage becomes one line.
        out.append({"session_id": sid, "date": date, "text": _one_line(chunk)})
    return out


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------


def collect_sessions(server: Server, cutoff: datetime, args) -> list[dict]:
    rows = server.sessions(cutoff, all_sessions=args.all_sessions)[: args.max_sessions]
    cwds = launch_cwds()

    def fetch(row: dict) -> dict:
        sid = str(row.get("session_id") or "")
        try:
            return server.detail(sid)
        except Exception as exc:  # one broken session must not sink the recap
            print(f"[cognee-recap] {sid}: detail failed ({str(exc)[:80]})", file=sys.stderr)
            return {}

    # One detail call per session, a few in flight: 25 sessions serially is
    # over a minute against a busy local server, a few seconds this way.
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        details = list(pool.map(fetch, rows))
    records = []
    for row, detail in zip(rows, details):
        rec = summarize_session(row, detail, cwds)
        if args.projects and not any(
            p.lower() in (rec["cwd"] or "").lower() for p in args.projects
        ):
            continue
        records.append(rec)
    return records


def in_window(passages: list[dict], session_ids: set[str], cutoff: datetime) -> list[dict]:
    """Passages learned inside the window.

    The header date is the day the learning was distilled and wins when present
    — a long-lived session can be active this week and still carry a learning
    from a month ago. Undated passages count when their session is in the
    window; passages with neither are dropped (nothing places them in time).
    """
    kept = []
    for p in passages:
        if p["date"]:
            when = parse_time(p["date"])
            if when and when >= cutoff:
                kept.append(p)
        elif p["session_id"] in session_ids:
            kept.append(p)
    return kept


def collect_timeline(
    server: Server, topic: str, cutoff: datetime, sessions: list[dict]
) -> list[dict]:
    """Dated events about ``topic``: graph passages + prompts that mention it."""
    by_id = {s["session_id"]: s for s in sessions}
    needle = topic.lower()
    events: list[dict] = []
    for p in server.passages(topic, TIMELINE_TOP_K):
        when = None
        sess = by_id.get(p["session_id"])
        if p["date"]:
            when = parse_time(p["date"])
        elif sess and sess.get("started_at"):
            when = parse_time(sess["started_at"])
        if when and when < cutoff:
            continue
        events.append(
            {
                # A header date is a calendar day, not an instant: kept as-is so
                # rendering does not shift "midnight UTC" into the previous local day.
                "time": p["date"] or (when.isoformat() if when else None),
                "kind": "learning",
                "session_id": p["session_id"],
                "project": sess["project"] if sess else "",
                "agent": agent_of(p["session_id"]) if p["session_id"] else "",
                "text": p["text"][:PASSAGE_CHARS],
            }
        )
    for s in sessions:
        for prompt in s["prompts"]:
            if needle in prompt.lower():
                events.append(
                    {
                        "time": s.get("last_activity_at"),
                        "kind": "prompt",
                        "session_id": s["session_id"],
                        "project": s["project"],
                        "agent": s["agent"],
                        "text": prompt[:PASSAGE_CHARS],
                    }
                )
    events.sort(key=lambda e: e["time"] or "")
    return events


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _group_by_project(records: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for rec in records:
        groups.setdefault(rec["project"] or "(no project)", []).append(rec)
    return sorted(groups.items(), key=lambda kv: (kv[0] == "(no project)", kv[0].lower()))


def _session_line(rec: dict, *, prompts: int) -> list[str]:
    start = parse_time(rec.get("started_at"))
    last = parse_time(rec.get("last_activity_at"))
    head = (
        f"- {_span(start, last)}"
        f"{' (' + rec['duration'] + ')' if rec['duration'] else ''}"
        f" · {rec['agent']} · {rec['prompt_count']} prompts · {rec['tool_calls']} tool calls"
        f"{' · ' + str(rec['errors']) + ' errors' if rec['errors'] else ''}"
        f"{' · ' + rec['status'] if rec['status'] in ('running', 'failed') else ''}"
    )
    lines = [head]
    if rec["label"]:
        lines.append(f'  "{rec["label"][:PROMPT_CHARS]}"')
    elif rec["tools"]:
        mix = ", ".join(f"{name} ×{n}" for name, n in rec["tools"].items())
        lines.append(f"  no prompts captured (host without prompt hooks) · recent tools: {mix}")
    if rec["files_edited"]:
        shown = rec["files_edited"][:8]
        more = len(rec["files_edited"]) - len(shown)
        lines.append("  edited: " + ", ".join(shown) + (f", +{more}" if more > 0 else ""))
    if prompts and len(rec["prompts"]) > 1:
        for prompt in rec["prompts"][-prompts:]:
            if prompt != rec["label"]:
                lines.append(f"  · {prompt[:PROMPT_CHARS]}")
    return lines


def render_standup(records: list[dict], label: str, cutoff: datetime) -> str:
    projects = {r["project"] for r in records if r["project"]}
    lines = [
        f"# Standup — {label} (since {_local(cutoff, '%Y-%m-%d %H:%M')}) · "
        f"{_plural(len(records), 'session')} · {_plural(len(projects), 'project')}",
        "",
    ]
    if not records:
        lines.append("_No coding-agent sessions in this window._")
        return "\n".join(lines)
    for project, recs in _group_by_project(records):
        cwd = next((r["cwd"] for r in recs if r["cwd"]), "")
        lines.append(f"## {project}{' — ' + _short_path(cwd, '') if cwd else ''}")
        for rec in recs:
            lines.extend(_session_line(rec, prompts=3))
        lines.append("")
    open_ones = [r for r in records if r["status"] in ("running", "abandoned") and r["prompts"]]
    if open_ones:
        lines.append("## Left open (last prompt of sessions that did not end cleanly)")
        for rec in open_ones[:6]:
            lines.append(f"- {rec['project'] or rec['agent']}: {rec['prompts'][-1][:PROMPT_CHARS]}")
        lines.append("")
    lines.append(
        "_Data: sessions + recent prompts/tool calls from the Cognee server; "
        "the detail endpoint returns each session's last 20 prompts and 20 tool calls._"
    )
    return "\n".join(lines)


def render_digest(records: list[dict], passages: list[dict], label: str, cutoff: datetime) -> str:
    lines = [f"# Digest — {label} (since {_local(cutoff, '%Y-%m-%d')})", ""]
    if not records and not passages:
        lines.append("_Nothing recorded in this window._")
        return "\n".join(lines)
    # Sessions by day, newest day first.
    by_day: dict[str, list[dict]] = {}
    for rec in records:
        day = _local(parse_time(rec.get("last_activity_at")), "%Y-%m-%d %a")
        by_day.setdefault(day, []).append(rec)
    total_prompts = sum(r["prompt_count"] for r in records)
    total_tools = sum(r["tool_calls"] for r in records)
    files: Counter = Counter()
    for rec in records:
        files.update(rec["files_edited"])
    lines.append(
        f"**{len(records)} sessions · {total_prompts} prompts · {total_tools} tool calls · "
        f"{len(files)} distinct files edited** across "
        + ", ".join(sorted({r["project"] for r in records if r["project"]}) or ["(no project)"])
    )
    lines.append("")
    for day in sorted(by_day, reverse=True):
        lines.append(f"## {day}")
        for project, recs in _group_by_project(by_day[day]):
            lines.append(f"### {project}")
            for rec in recs:
                lines.extend(_session_line(rec, prompts=2))
        lines.append("")
    if files:
        lines.append("## Most-edited files")
        for path, n in files.most_common(10):
            lines.append(f"- {path} ({_plural(n, 'session')})")
        lines.append("")
    if passages:
        lines.append("## Learnings recorded in the graph")
        for p in passages:
            stamp = f"{p['date']} · " if p["date"] else ""
            lines.append(f"- {stamp}{p['text'][:PASSAGE_CHARS]}")
        lines.append("")
    lines.append(
        "_Summarise the above into: what shipped, what was decided, what is still open. "
        "Learnings come from graph passages stamped with the session they were distilled from._"
    )
    return "\n".join(lines)


def render_timeline(topic: str, events: list[dict], label: str) -> str:
    lines = [f"# Timeline — {topic!r} · {label} · {len(events)} events", ""]
    if not events:
        lines.append(
            "_Nothing about this topic in the window. "
            "Try a broader `--since` or a different phrasing._"
        )
        return "\n".join(lines)
    current_day = None
    for ev in events:
        day, clock = _event_day(ev["time"])
        if day != current_day:
            lines.append(f"## {day}")
            current_day = day
        where = " · ".join(x for x in (ev["project"], ev["agent"]) if x)
        tag = "learned" if ev["kind"] == "learning" else "asked"
        lines.append(f"- {clock} [{tag}{' · ' + where if where else ''}] {ev['text']}")
    lines.append("")
    lines.append(
        "_`learned` = knowledge-graph passage distilled from that session; "
        "`asked` = a prompt in the window that names the topic._"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cognee-recap.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    def common(p):
        p.add_argument(
            "--since",
            default=None,
            help="24h, 7d, 2w, today, yesterday, week, month, all, or a date",
        )
        p.add_argument("--json", action="store_true", help="machine-readable output")
        p.add_argument(
            "--all-sessions", action="store_true", help="include non-coding-agent sessions"
        )
        p.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS)
        p.add_argument("--projects", default="", help="comma-separated cwd substrings to keep")
        p.add_argument(
            "--session-key",
            default="",
            help="host session id (when several launches share a directory)",
        )

    common(sub.add_parser("standup", help="what happened since yesterday, per project"))
    common(sub.add_parser("digest", help="the week: sessions per day, files, graph learnings"))
    tl = sub.add_parser("timeline", help="chronological view of a topic")
    tl.add_argument("topic")
    common(tl)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.projects = [p.strip() for p in (args.projects or "").split(",") if p.strip()]
    cutoff, label = parse_since(args.since or DEFAULT_SINCE[args.mode])
    server = Server(args.session_key)
    try:
        records = collect_sessions(server, cutoff, args)
        passages: list[dict] = []
        events: list[dict] = []
        if args.mode == "digest":
            try:
                found = server.passages(DIGEST_QUERY, TIMELINE_TOP_K)
                passages = in_window(found, {r["session_id"] for r in records}, cutoff)
                if found and not passages:
                    print(
                        f"[cognee-recap] {len(found)} graph learnings matched but none are "
                        "dated in the window; /cognee-sync distils recent sessions into the graph.",
                        file=sys.stderr,
                    )
            except Exception as exc:
                print(
                    f"[cognee-recap] graph passages unavailable ({str(exc)[:80]})", file=sys.stderr
                )
        elif args.mode == "timeline":
            events = collect_timeline(server, args.topic, cutoff, records)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(
            f"[cognee-recap] Cognee server unreachable at {server.url} ({str(exc)[:100]}) — "
            "no recap; retry once it is back (cognee-doctor.sh --json shows the mode/URL).",
            file=sys.stderr,
        )
        return 1

    if args.json:
        payload = {
            "mode": args.mode,
            "since": cutoff.isoformat(),
            "label": label,
            "sessions": records,
        }
        if args.mode == "digest":
            payload["learnings"] = passages
        if args.mode == "timeline":
            payload["topic"] = args.topic
            payload["events"] = events
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if args.mode == "standup":
        print(render_standup(records, label, cutoff))
    elif args.mode == "digest":
        print(render_digest(records, passages, label, cutoff))
    else:
        print(render_timeline(args.topic, events, label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
