#!/usr/bin/env python3
"""Search session + trace + agent guidance + graph for context relevant to the user's prompt.

Runs on the UserPromptSubmit hook. Two requests to ``/api/v1/recall`` in flight
together: ``scope=["graph"]`` with ``HYBRID_COMPLETION`` and ``only_context=True``
but WITHOUT the session id, and ``scope=["session_context"]`` with it. On cognee
>= 1.6.0 the graph item's ``text`` is the full LLM input the completion would
have received; sent without a session id it carries no conversation history,
and ``_recall_text`` cuts the question template away and shrinks the retrieved
context (own-session passages dropped, passages capped, empty entity bullets
pruned) before it is injected — the agent already holds the conversation, so
re-sending it only cost tokens (SDK-904). The session_context item is the
server-rendered guidance block, capped server-side. A deterministic code-graph
lane is added only on identifier-shaped prompts, and injected only when it
found something.

Configuration:
    Resolves session state via Cognee HTTP endpoints.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import sys
import time

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
import _plugin_common
from _cloud_worker import enabled as cloud_enabled
from _evidence import evidence_items, render_item, write_receipt
from _injection_budget import fit_sections, injection_limit
from _plugin_common import (
    DEFINITIVE_FAILURE_STATES,
    _float_env,
    authed_liveness,
    buffered_saves_segments,
    cached_readable_datasets,
    clear_payment_required,
    clear_slow_streak,
    cross_dataset_search_command,
    elapsed_ms,
    get_session_key,
    hook_log,
    is_observer_child,
    load_resolved,
    mark_recall_slow,
    mark_server_ready,
    notify,
    other_readable_datasets,
    outage_header,
    phase_begin,
    phase_end,
    probe_health,
    quiet_hook_output,
    read_and_reset_save_counter,
    read_connection_state,
    recall_slow_until,
    recall_via_http,
    record_payment_required,
    record_slow_probe,
    resolve_active_dataset_ids,
    resolve_runtime_mode,
    resolve_session_key_from_payload,
    same_connection_target,
    saves_segment,
    server_ready_hint,
    set_session_key,
    slow_streak_threshold,
    warmup_backlog,
    with_base_url_notice,
    write_connection_state,
)
from _recall_http import DOWN, SLOW, classify_transport_exception
from _recall_text import (
    PASSAGE_SEPARATOR,
    MemoryTags,
    begin_turn,
    code_facts,
    credit_enabled,
    credit_instruction,
    format_code_facts,
    has_credited,
    is_session_transcript,
    passage_origin,
    split_passages,
    split_sections,
    trim_recall_text,
)
from config import get_dataset, get_session_id, load_config

#: Per-field caps for recall-audit.log lines (characters).
_AUDIT_PROMPT_CHARS = 2000
_AUDIT_CONTEXT_CHARS = 4000


def _audit_clip(value, limit: int) -> str:
    """Head of ``value`` for the audit log, marked when clipped."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[+{len(text) - limit} chars]"


TOP_K = 5
TRUNCATE_ANSWER = 500
TRUNCATE_RETURN = 400
# Smallest deadline worth dispatching; with less budget than this, nothing is
# fired rather than sending every scope a doomed request.
MIN_SCOPE_TIMEOUT = 0.2

# Cross-dataset search hint. Recall reads the active dataset only, and whether
# what came back actually answers the user is a judgement only the model can
# make — graph retrieval is nearest-neighbour, so it returns *something* from
# any populated dataset, and "zero hits" never happens in practice. So every
# prompt the server answered carries the list of the other readable datasets
# and how to run a one-off graph search on one (not a switch), worded for the
# model to act on only when memory did not answer. The listing comes from the
# per-plugin cache (``cached_readable_datasets``) and is refreshed only when
# stale, inside what is left of the recall budget. Every other readable dataset
# is named: nothing ranks them, so the user picks. The model must not switch on
# its own, but a bare "Do not switch datasets." also beat users who asked for a
# switch in plain words, so the line names the switch skill for that case.
# COGNEE_RECALL_DATASET_HINT=off disables the hint.


def _dataset_hint_enabled() -> bool:
    raw = os.environ.get("COGNEE_RECALL_DATASET_HINT", "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def _format_dataset_hint(active: str, others: list) -> str:
    rows = "\n".join(f"  - {row.get('name', '')} [{row.get('id', '')}]" for row in others)
    return (
        f"Other Cognee datasets you can search (active: {active or 'unknown'}):\n{rows}\n"
        "Memory above was recalled from the active dataset only. If the user is asking to recall "
        "something and that context does not answer it, do not conclude that memory has nothing: "
        "ask which of these datasets to search, with AskUserQuestion (single select, the dataset "
        'names as options; more than four → the first three plus a "More…" option and page on '
        "the next question; say in the question that they can also decline). Do not switch "
        "datasets on your own; if the user asks to switch, use "
        "/cognee-memory:cognee-switch-datasets. Then run the graph-only search on their choice "
        "and say which dataset the answer came from:\n"
        f"  {cross_dataset_search_command()}"
    )


def _other_datasets_hint(active: str, active_ids: list, service_url: str, deadline: float) -> str:
    """The hint block, or "" when there is no other dataset to offer.

    Cache first; one bounded network refresh only when the cache is stale and
    the recall budget still has room for an honest attempt.
    """
    remaining = deadline - time.monotonic()
    rows = cached_readable_datasets(
        service_url=service_url,
        refresh=remaining >= MIN_SCOPE_TIMEOUT,
        timeout=max(MIN_SCOPE_TIMEOUT, min(2.0, remaining)),
    )
    others = other_readable_datasets(rows, active, active_ids)
    if not others:
        return ""
    hook_log("recall_dataset_hint", {"active": active, "offered": len(others)})
    return _format_dataset_hint(active, others)


def _load_session_id() -> str:
    resolved = load_resolved(identity=False)
    session_id = resolved.get("session_id", "")
    if not session_id:
        config = load_config()
        session_id = get_session_id(config)
    return session_id


def _format_entry(entry: dict) -> str:
    """Format a single recall result according to its _source tag."""
    source = entry.get("source", "")

    if source == "graph_context":
        # One item per dataset. On cognee >= 1.6.0 its ``text`` is the full LLM
        # input the completion would have received; ``_run`` has already cut
        # it down to the retrieved context (``trim_recall_text``), so what is
        # left is injected as-is. Older servers put the bare retrieval context
        # there, which the trimmer passes through untouched.
        content = str(entry.get("text", "") or entry.get("content", ""))
        return f"[cognee-memory]\n{content}"

    if source == "session_context":
        content = str(entry.get("content", "") or entry.get("text", ""))
        return f"[agent-guidance]\n{content}"

    if source == "code":
        # Deterministic code-graph facts (ResponseCodeEntry): ``text`` is the
        # server's JSON ``query_facts`` payload, rendered one line per symbol;
        # any other shape is injected as-is.
        content = str(entry.get("text", "") or entry.get("content", ""))
        facts = code_facts(content)
        if facts:
            content = format_code_facts(facts)
        return f"[code-graph]\n{content}"

    if source == "trace":
        origin = entry.get("origin_function", "?")
        status = entry.get("status", "")
        feedback = entry.get("session_feedback", "")
        mrv = entry.get("method_return_value", "")
        if isinstance(mrv, (dict, list)):
            mrv = json.dumps(mrv, default=str)
        mrv = str(mrv)[:TRUNCATE_RETURN]
        parts = [f"[trace] {origin} — {status}"]
        if feedback and not re.fullmatch(r"\s*\w+ succeeded\.?\s*", str(feedback)):
            parts.append(f"  feedback: {feedback}")
        if mrv:
            parts.append(f"  output: {mrv}")
        return "\n".join(parts)

    # session (QA) or generic
    q = entry.get("question", "")
    a = entry.get("answer", "")
    t = entry.get("time", "")
    lines = []
    if q:
        lines.append(f"[{t}] Q: {q}")
    if a:
        a_short = a[:TRUNCATE_ANSWER] + "..." if len(a) > TRUNCATE_ANSWER else a
        lines.append(f"A: {a_short}")
    return "\n".join(lines)


def _line_entries(text: str) -> list[str]:
    """Split fact/prose lines while keeping fenced code and entity records whole."""
    if len(json.dumps(text)) <= 1024:
        return [text.strip()] if text.strip() else []
    entries, pending = [], []
    fence = ""
    for line in text.strip().splitlines():
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence:
            pending.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                entries.append("\n".join(pending))
                pending, fence = [], ""
        elif marker:
            pending.append(line)
            fence = marker[1]
        elif stripped.startswith("### "):
            if pending:
                entries.append("\n".join(pending))
            pending = [line]
        elif pending and pending[0].lstrip().startswith("### ") and stripped:
            pending.append(line)
        elif stripped:
            entries.append("\n".join(pending + [line]))
            pending = []
    if pending:
        entries.append("\n".join(pending))
    return entries


def _entry_origin(source: str, entry: dict) -> str:
    """Where a non-graph recall item came from, in a few words for its tag."""
    if source == "code":
        return "code graph"
    if source == "session_context":
        return "agent guidance"
    if source == "graph_context":
        return "Cognee memory"
    if source == "trace":
        return f"agent trace, {entry.get('origin_function', '?')}"
    day = str(entry.get("time", "") or "")[:10]
    return f"earlier session turn, {day}" if day else "earlier session turn"


def _injection_sections(
    by_source: dict, tags: MemoryTags | None = None, labels: dict | None = None
) -> list[dict]:
    """Expose bare records; shared labels belong to one section heading.

    With ``tags``, every item carries its credit tag (SDK-1071): an evidence
    item, passage, session turn or trace on its own first line, a block of
    entities, facts or other lines on its heading. Tags are only assigned
    here; ``labels`` collects each ``[mN · origin]`` so the caller can count
    the ones that survive the budget and dedupe as shown.
    """
    grouped = {}

    def label(kind, origin, text, ident=""):
        if tags is None:
            return ""
        tag = tags.assign(kind, origin, text, ident)
        text = f"[{tag} · {origin}]"
        if labels is not None:
            labels[text] = tag
        return text

    def tagged(text, kind, origin, ident=""):
        mark = label(kind, origin, text, ident)
        return f"{mark}\n{text}" if mark else text

    def add_block(source, kind, title, priority, entries, result_id, mark):
        """One tag for a block of entries: on the heading, or on the block's first
        entry when an earlier result already gave the section its heading."""
        if mark and entries:
            if (source, kind) in grouped:
                entries = [f"{mark}\n{entries[0]}"] + entries[1:]
            else:
                title = f"{title} {mark}"
        add(source, kind, title, priority, entries, result_id)

    def add(source, kind, title, priority, entries, result_id, separator="\n\n"):
        section = grouped.setdefault(
            (source, kind),
            {
                "title": title,
                "entries": [],
                "priority": priority,
                "source": source,
                "entry_ids": {},
                "separator": separator,
            },
        )
        for entry in entries:
            if entry.strip():
                section["entries"].append(entry)
                section["entry_ids"][entry] = result_id

    for source, title, priority in (
        ("code", "=== Code graph facts ===", 50),
        ("graph_context", "=== Cognee memory ===", 40),
        ("session_context", "=== Active agent guidance ===", 30),
        ("session", "=== Prior session turns ===", 20),
        ("trace", "=== Prior agent trace ===", 10),
    ):
        for result_id, entry in enumerate(by_source.get(source, [])):
            items = evidence_items(entry)
            available = [(render_item(item), item) for item in items or []]
            available = [(text, item) for text, item in available if text]
            # Evidence replaces the legacy text only when something can be shown;
            # a result whose items are all unavailable keeps its plain text.
            if source == "graph_context" and available:
                for text, item in available:
                    derived = (item.get("source") or {}).get("derived")
                    origin = "derived graph evidence" if derived else "source evidence"
                    # Keyed by its citation: the same evidence keeps its tag.
                    text = tagged(text, "evidence", origin, str(item.get("citation") or ""))
                    add(source, "evidence", title, priority, [text], result_id)
                    grouped[(source, "evidence")].setdefault("evidence", {})[text] = item
                continue
            if source == "graph_context":
                text = str(entry.get("text") or entry.get("content") or "")
                parts = split_sections(text)
                if any(parts.get(k) is not None for k in ("passages", "entities", "facts")):
                    for kind, heading, rank in (
                        ("preamble", "", 40),
                        ("entities", "## Relevant entities", 41),
                        ("facts", "## Related facts", 41),
                        ("passages", "## Relevant passages", 40),
                    ):
                        if parts.get(kind):
                            label_ = f"=== Cognee memory: {kind} ===\n[cognee-memory]"
                            if heading:
                                label_ += "\n" + heading
                            if kind != "passages":
                                entries = _line_entries(parts[kind])
                                origin = {
                                    "preamble": "Cognee memory",
                                    "entities": "knowledge graph entities",
                                    "facts": "knowledge graph facts",
                                }[kind]
                                mark = label(kind, origin, parts[kind])
                                add_block(source, kind, label_, rank, entries, result_id, mark)
                                continue
                            passages = [
                                tagged(p, "passage", passage_origin(p))
                                for p in split_passages(parts[kind])
                            ]
                            # Raw turns bridged from other sessions are the bulk and
                            # the least distilled: across every dataset they go before
                            # session learnings and remembered documents.
                            add(
                                source,
                                kind,
                                label_,
                                rank,
                                [p for p in passages if not is_session_transcript(_untag(p))],
                                result_id,
                                PASSAGE_SEPARATOR,
                            )
                            add(
                                source,
                                "transcripts",
                                "=== Cognee memory: transcripts ===\n[cognee-memory]\n"
                                "## Passages from other sessions",
                                rank - 1,
                                [p for p in passages if is_session_transcript(_untag(p))],
                                result_id,
                                PASSAGE_SEPARATOR,
                            )
                    continue
            formatted = _format_entry(entry)
            origin = _entry_origin(source, entry)
            if source in {"graph_context", "code", "session_context"}:
                prefix, _, body = formatted.partition("\n")
                mark = label(source, origin, formatted)
                block = _line_entries(body)
                add_block(source, "body", title + "\n" + prefix, priority, block, result_id, mark)
            else:
                # An earlier turn or trace is known by its server id, so it keeps
                # its tag when recalled again.
                ident = str(entry.get("qa_id") or entry.get("trace_id") or "")
                add(
                    source,
                    "body",
                    title,
                    priority,
                    [tagged(formatted, source, origin, ident)],
                    result_id,
                )
    return list(grouped.values())


def _untag(text: str) -> str:
    """``text`` without the credit tag line ``_injection_sections`` put on it."""
    first, sep, rest = text.partition("\n")
    if sep and first.startswith("[m") and " · " in first and first.endswith("]"):
        return rest
    return text


def _recall_output(context: str, notice: str = "") -> dict:
    """The exact printed envelope, including both user- and model-visible notices."""
    header = context.split("\n", 1)[0]
    return with_base_url_notice(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
                "systemMessage": header,
            }
        },
        "UserPromptSubmit",
        notice=notice,
    )


def _report_hint(prompt: str) -> str:
    if os.environ.get("COGNEE_REPORT_HINT", "true").strip().lower() in {"0", "false", "no", "off"}:
        return ""
    import re

    evaluation = re.search(
        r"\b(?:evaluate|grade|assess|audit|check on)\s+cognee\b|"
        r"\bhow\s+is\s+cognee\s+(?:doing|performing)\b|"
        r"\bis\s+cognee\s+worth\s+it\b|\bdid\s+the\s+update\s+help\b|"
        r"\bcognee\s+stats\b|\bmemory\s+quality\b",
        prompt,
        re.IGNORECASE,
    )
    return "Use /cognee-report for an evaluation" if evaluation else ""


def _bounded_notice(header: str, notice: str) -> str:
    """Keep a visible diagnostic even when a long URL/pattern exhausts the budget."""
    if len(json.dumps(_recall_output(header, notice))) + 1 <= injection_limit():
        return notice
    hook_log("hook_notice_failed", {"source": "injection_budget", "error": notice})
    return "Cognee: configuration notice omitted to fit context; inspect hook.log for details."


def _outage_output(state: str, notice: str = "") -> dict:
    """Envelope for a prompt whose recall was skipped: see ``outage_header``."""
    session_id = _load_session_id()
    saves = read_and_reset_save_counter(session_id) if session_id else {}
    header = outage_header(state, saves, warmup_backlog(), "; ")
    return _recall_output(header, _bounded_notice(header, notice))


def _has_entry_content(entry: dict) -> bool:
    """Return True when a recall entry has useful content to inject."""
    source = entry.get("source", "")
    if source == "graph_context":
        if evidence_items(entry):
            return True
        return bool(str(entry.get("content", "") or entry.get("text", "")).strip())
    if source == "session_context":
        return bool(str(entry.get("content", "") or entry.get("text", "")).strip())
    if source == "code":
        # A ``query_facts`` payload with no facts is a miss, not a hit: the
        # server answers the lane with an empty list when the seed resolves
        # to nothing, and injecting that JSON told the model nothing (SDK-904).
        content = str(entry.get("text", "") or entry.get("content", ""))
        facts = code_facts(content)
        if facts is not None:
            return bool(facts)
        return bool(content.strip())
    if source == "trace":
        fields = ("origin_function", "status", "session_feedback", "method_return_value")
    else:
        fields = ("question", "answer")
    return any(str(entry.get(field, "") or "").strip() for field in fields)


async def _run(prompt: str, cwd: str = "", notice: str = "") -> dict | None:
    """The recall, with its ``recalling`` phase ended on every exit.

    ``_recall`` returns early on a cached outage, a failed probe or a missing
    session id, and can raise; without this wrapper those paths left the
    phase active and the status row spinning on ``recalling…`` until the
    marker went stale (90 s). An exit with no count is a failed recall.
    """
    phase_begin("recalling")
    total: int | None = None
    try:
        output, total = await _recall(prompt, cwd, notice=notice)
        return output
    finally:
        phase_end("recalling", count=total or 0, ok=total is not None)


async def _recall(prompt: str, cwd: str = "", notice: str = "") -> tuple[dict | None, int | None]:
    if credit_enabled(_plugin_common.CITE_MEMORY_DEFAULT):
        # First, so a recall that returns early leaves this turn with no tags
        # and the Stop hook cannot check the answer against the previous
        # turn's (SDK-1071).
        try:
            begin_turn(_plugin_common._PLUGIN_DIR, get_session_key())
        except Exception as exc:
            hook_log("memory_tags_write_failed", {"error": str(exc)[:200]})
    config = load_config()
    runtime = resolve_runtime_mode()
    hook_log(
        "mode_decision",
        {
            "hook": "session-context-lookup",
            "mode": runtime["mode"],
            "base_url": runtime.get("base_url", ""),
            "url_source": runtime.get("url_source", ""),
            "key_source": runtime.get("key_source", ""),
            "api_key_present": runtime.get("api_key_present", False),
        },
    )
    # Readiness gate, redesigned (SDK-356): the recall attempt itself is the
    # probe. A fresh "ready" marker or a merely-stale/unknown state goes
    # STRAIGHT to recall — a successful scope call is an authenticated,
    # real-workload confirmation that beats any synthetic /health check, and
    # the recall budget already bounds the worst case. Probing survives only
    # as a cheap re-entry gate while the marker holds a KNOWN failure state,
    # so a confirmed-bad backend costs one bounded probe per prompt instead of
    # the full budget.
    service_url = runtime.get("base_url", "")
    probe_timeout = _float_env("COGNEE_READY_PROBE_TIMEOUT", 3.0)
    prior = read_connection_state()
    # Permissive on purpose: "same target" unless the two URLs provably differ,
    # so a recorded state still applies when a URL is unknown. Mirrors the
    # renderer's _url_mismatch (equivalence pinned by
    # tests/test_connection_target_match.py).
    prior_same_target = same_connection_target(service_url, str(prior.get("base_url") or ""))
    prior_state = str(prior.get("state") or ("ready" if prior.get("ready_at") else ""))
    # Ignore legacy not_responding markers: a slow recall is not an outage.
    known_bad = prior_same_target and prior_state in (
        "auth_failed",
        "unreachable",
        "server_error",
    )
    if known_bad and cloud_enabled(runtime):
        hook_log("recall_skipped_not_ready", {"state": prior_state, "cached": True})
        # The notice is one-shot (already consumed by main); never drop it here.
        return _outage_output(prior_state, notice), None
    if known_bad:
        # Prefer an AUTHENTICATED probe so a bad/expired key is classified as
        # auth_failed instead of being masked as "ready" by an unauthenticated
        # /health 200. Fall back to /health only when the authed probe can't
        # classify (no key, or the endpoint is absent on an older server).
        state = authed_liveness(service_url, timeout=probe_timeout)
        if state == "unknown":
            health = probe_health(service_url, timeout=probe_timeout)
            state = {"ready": "ready", "down": "unreachable"}.get(health, health)
        if state == "ready":
            mark_server_ready(service_url)
            clear_slow_streak(service_url)
            # fall through to recall below
        else:
            if state in DEFINITIVE_FAILURE_STATES:
                # A definitive verdict: refresh/replace the recorded failure.
                write_connection_state(state, service_url, detail="authed liveness probe")
                clear_slow_streak(service_url)
            # "slow"/"unknown" from the probe is NO verdict — keep the recorded
            # state untouched rather than promote a timeout to a failure.
            hook_log("recall_skipped_not_ready", {"base_url": service_url, "state": state})
            # Report the outage instead of going quiet: the recorded failure
            # names it when the probe itself was inconclusive.
            return (
                _outage_output(
                    state if state in DEFINITIVE_FAILURE_STATES else prior_state, notice
                ),
                None,
            )

    session_id = _load_session_id()
    if not session_id:
        hook_log("no_session_id", {"event": "context_lookup"})
        return None, None

    # NOTE: the warmup-buffer drain deliberately does NOT run here. This hook
    # is synchronous on the keystroke->answer path, and replaying N buffered
    # entries (~1s of server work each) stalled the prompt for 10-30s after any
    # long turn (#298). The async sibling hook on this same event
    # (store-user-prompt.py) drains instead; improve/SessionEnd re-drain too.
    saves_last_turn = read_and_reset_save_counter(session_id)

    # Two requests for memory (SDK-904). The graph recall is sent WITHOUT the
    # session id: with it, a cognee >= 1.6.0 ``only_context`` completion
    # prepends the session's last ten Q&A pairs in full — the agent's own
    # previous turns, already in its context window — which was ~60% of a
    # mid-session payload. Without it the item is the question template around
    # the retrieved context, and ``trim_recall_text`` keeps only the context.
    # The guidance block (the ``session_context`` layer distilled from the
    # trace layer) is small and useful, so it is fetched on its own, with the
    # session id, as the server-rendered block. Scope, type and only_context
    # stay explicit: the server's ``auto`` scope would add raw session entries
    # next to the prompt (or short-circuit the graph on a session hit), and an
    # unpinned type lets the router pick CHUNKS, which never builds a prompt.
    # HYBRID_COMPLETION combines BM25 + vector + graph retrieval; the LLM
    # completion itself is skipped server-side. The code lane below is the
    # only other request, and only on prompts that arm it. The list order is
    # the canonical reporting order (per_scope, logs).
    results: list = []
    scope_specs = [
        (["graph"], "HYBRID_COMPLETION", None),
        (["session_context"], None, "qa"),
    ]
    # Additive code-graph lane (cognee >= 1.5.3). Fires only when the prompt
    # carries an identifier-shaped token AND the cwd sits inside a repo the
    # user indexed via cognee-index-repo.sh — never on conversational prompts,
    # never as a replacement for the semantic scopes. The server keeps this
    # scope explicit-only (scope=auto never implies it), so the gate lives
    # here.
    code_lane = {}
    try:
        from _code_graph import auto_code_lane

        code_lane = auto_code_lane(prompt, cwd) or {}
    except Exception as exc:
        hook_log("code_lane_gate_error", {"error": str(exc)[:200]})
    if code_lane:
        scope_specs.append((["code"], None, None))
        hook_log(
            "code_lane_armed",
            {
                "identifier": code_lane.get("identifier", ""),
                "dataset": code_lane.get("dataset", ""),
            },
        )

    # Per-scope instrumentation (WS7 observability): capture {hits, elapsed_ms}
    # for every scope, keyed by its stable label. Pre-seed all scopes as
    # skipped, in canonical order and before the breaker-open branch below can
    # blank scope_specs, so the event always carries the full set; each scope
    # that actually runs overwrites its own record. Purely additive: it must
    # not touch recall results, ordering, or control flow, and must never raise
    # into the keystroke->answer path.
    per_scope: dict[str, dict] = {
        scope_list[0]: {"hits": 0, "elapsed_ms": 0, "skipped": True}
        for scope_list, _qtype, _profile in scope_specs
    }

    # Hard time-box: this hook is on the keystroke->answer path, so recall must
    # never be the long pole. Every scope is dispatched at once with the same
    # deadline, the whole budget, so the recall can never outlast it and no
    # scope waits behind another. A scope that overruns is recorded as zero
    # hits; partial results are fine. One knob on purpose: with the scopes
    # concurrent, a per-scope timeout and a whole-recall budget would bound the
    # very same interval. COGNEE_RECALL_TIMEOUT is NOT read here — it still
    # bounds the explicit cognee-search path (_cognee_client.py).
    #
    # Defaults are 45s for cloud and 20s for local graph recall.
    # Graph search time grows with the dataset and with the round trip to a
    # remote (cloud) server, so a cap tuned for a small local graph silently
    # drops graph memory once either grows.
    recall_budget = _float_env("COGNEE_RECALL_BUDGET", 45.0 if cloud_enabled(runtime) else 20.0)
    # After slow_streak_threshold() timeout-only prompts the server is treated as
    # slow: recall gets a few seconds until a recall answers or the window ends,
    # so a hung server cannot cost the full budget on every prompt.
    slow_until = recall_slow_until(service_url)
    recall_slow = time.time() < slow_until
    if recall_slow:
        recall_budget = min(recall_budget, _float_env("COGNEE_RECALL_SLOW_BUDGET", 5.0))
        hook_log("recall_budget_reduced", {"budget_s": recall_budget})
    recall_start = time.monotonic()
    budget_deadline = recall_start + recall_budget
    # Respect the shared circuit breaker: when the server has been failing (tripped
    # by the explicit recall path), skip this per-prompt recall rather than hammering
    # a down backend on every keystroke.
    try:
        from _cognee_client import breaker_open

        _bopen, _bretry = breaker_open(service_url)
    except Exception:
        _bopen, _bretry = False, 0
    if _bopen:
        hook_log("recall_breaker_open", {"retry_in": _bretry})
        scope_specs = []
    # Health accounting for this prompt's recall attempts (the attempt IS the
    # probe): a scope that returns is proof of life; a refused connection is
    # proof of death; timeouts alone are no verdict and only feed the streak.
    scopes_ok = 0  # calls that returned (even empty — the server answered)
    scopes_answered_err = 0  # HTTP-level errors: reachable, but not healthy
    scope_timeouts = 0
    server_down = False
    auth_rejected = False  # 401/403: the server answered and rejected OUR key
    payment_refused = False  # 402: the tenant cannot pay for this recall
    server_errors = 0  # 5xx answers: reachable but failing

    # Below the floor a call cannot return anything useful, so nothing is
    # dispatched rather than firing requests with a doomed deadline.
    remaining = budget_deadline - time.monotonic()
    if scope_specs and remaining < MIN_SCOPE_TIMEOUT:
        hook_log("recall_budget_exceeded", {"collected": 0})
        scope_specs = []
    # Clamped into [0, budget]: on a coarse clock (Windows) ``remaining`` can
    # read a few ULPs above the budget when no tick has passed since the start.
    scope_timeout = min(recall_budget, max(remaining, 0.0))

    # Everything the requests need is resolved once, up front, on the event
    # loop thread: the dataset routing reads plugin state files, and the answer
    # is the same for every scope. The code lane searches the indexed repo's own
    # (narrow) dataset with a structured query; every other scope keeps the
    # session dataset. Shared memory addresses the session dataset by UUID:
    # graph-only recall spans the canonical parent-owned copy plus any readable
    # same-named ones, while a scope that includes session history stays bound
    # to the ONE dataset the session writes to. The code dataset stays
    # name-addressed — it is this repo's own dataset.
    session_dataset = get_dataset(config)
    write_id, read_ids = resolve_active_dataset_ids() if scope_specs else ("", [])

    async def _dispatch(scope_list: list, qtype, context_profile):
        """One scope's request, run off the event loop.

        Returns ``(dataset, part, exc, elapsed_ms)``; never raises, so one
        failing scope cannot take the others down with it. ``elapsed_ms`` is
        measured around the call and recorded even when it errored.
        """
        is_code_scope = bool(code_lane) and scope_list == ["code"]
        scope_dataset = code_lane["dataset"] if is_code_scope else session_dataset
        scope_code_query = code_lane["code_query"] if is_code_scope else None
        if is_code_scope:
            scope_dataset_ids = []
        else:
            scope_dataset_ids = (
                read_ids if scope_list == ["graph"] else [write_id] if write_id else []
            )
        # The graph recall carries no session id (see scope_specs): the
        # history it would add is the agent's own conversation. Only the
        # guidance request needs the session.
        scope_session_id = "" if scope_list == ["graph"] else session_id
        part, exc = None, None
        t0 = time.monotonic()
        try:
            # recall_via_http blocks on urllib (and bounds itself with a
            # daemon thread), so it runs in a worker; the name is resolved at
            # call time so the module attribute stays the seam.
            part = await asyncio.to_thread(
                recall_via_http,
                prompt,
                session_id=scope_session_id,
                top_k=TOP_K,
                scope=scope_list,
                only_context=True,
                search_type=qtype,
                context_profile=context_profile,
                dataset=scope_dataset,
                dataset_ids=scope_dataset_ids,
                code_query=scope_code_query,
                timeout=scope_timeout,
                # Only this hook renders evidence items; other graph callers skip the cost.
                evidence_mode="context" if "graph" in scope_list else None,
            )
        except Exception as caught:
            exc = caught
        return scope_dataset, part, exc, round((time.monotonic() - t0) * 1000, 1)

    outcomes = await asyncio.gather(
        *(_dispatch(scope_list, qtype, profile) for scope_list, qtype, profile in scope_specs)
    )

    # Fold every scope's outcome in, in canonical order, so the injected
    # context and the logs read the same whichever request answered first.
    for (scope_list, _qtype, _profile), (scope_dataset, part, exc, elapsed) in zip(
        scope_specs, outcomes
    ):
        # hits = raw count from this scope's call (pre-bucketing/filtering).
        per_scope[scope_list[0]] = {"hits": len(part or []), "elapsed_ms": elapsed}
        if exc is None:
            if part:
                results.extend(part)
            scopes_ok += 1
            continue
        import urllib.error as _urlerr

        verdict = classify_transport_exception(exc)
        is_404 = isinstance(exc, _urlerr.HTTPError) and exc.code == 404
        if is_404 and scope_list == ["session_context"]:
            # The server answered: no session guidance exists for this session
            # or dataset yet (a fresh install, a dataset without a graph).
            # Authoritative empty, same as ``_recall_http`` reads a 404 — not a
            # failure, and the server was reached, so the request counts as ok.
            hook_log("recall_guidance_absent", {"scope": scope_list, "dataset": scope_dataset})
            scopes_ok += 1
            continue
        if is_404 and scope_list == ["graph"]:
            # A dataset nobody has written to yet has no graph, and the
            # server answers the graph scope with 404 (DatasetNotFound)
            # until the first cognify lands. On a fresh install that is
            # every prompt of the first session — expected, not an error:
            # keep it out of recall_error and the health accounting
            # (scopes_answered_err) so real failures stay visible (SDK-469).
            # It IS an answer, though: the server was reached and said so. Since
            # memory became one request (SDK-741) nothing else answers on such a
            # prompt, so this must count as ok or the ready marker, the breaker
            # and the dataset hint would all read a fresh dataset as an outage.
            hook_log("recall_graph_not_built", {"scope": scope_list, "dataset": scope_dataset})
            scopes_ok += 1
            continue
        if isinstance(exc, _urlerr.HTTPError):
            scopes_answered_err += 1
            if exc.code in (401, 403):
                auth_rejected = True
            elif exc.code == 402:
                payment_refused = True
            elif exc.code >= 500:
                server_errors += 1
        elif verdict == SLOW:
            scope_timeouts += 1
        elif verdict == DOWN:
            server_down = True
        hook_log(
            "recall_error",
            {"scope": scope_list, "error": str(exc)[:200], "verdict": verdict},
        )
    # Status-line credits: a 402 is the server refusing to pay for THIS recall,
    # the one positive exhaustion signal the plugin sees. Note it on the
    # tenant's marker entry; a recall that got through clears the note. Both
    # are best-effort and never raise.
    if payment_refused:
        record_payment_required("recall")
    elif scopes_ok:
        clear_payment_required()

    # The scopes were all in flight together, so there is nothing left to cut
    # short; these mark the prompt-level verdict for the health accounting.
    if server_down:
        # Positively absent (refused/DNS): every request failed in milliseconds.
        hook_log("recall_server_down", {"base_url": service_url})
    if auth_rejected:
        # Every scope shares the same API key, so every request drew the same
        # 401/403.
        hook_log("recall_auth_rejected", {"base_url": service_url})

    # Fold this prompt's recall outcomes back into the shared health state.
    # Best-effort: accounting must never break the keystroke->answer path.
    try:
        if server_down:
            # Suppress the write during a genuine cold-start warm-up: a refused
            # connection with no prior ready marker for this URL is likely the
            # server still launching/migrating — stay quiet rather than flash a
            # false red (and don't feed the breaker with warm-up refusals).
            warming = not (prior_state == "ready" and prior_same_target)
            if not warming:
                write_connection_state(
                    "unreachable", service_url, detail="connection refused during recall"
                )
                clear_slow_streak(service_url)
                try:
                    from _cognee_client import record_failure as _breaker_failure

                    _breaker_failure(
                        "connection refused",
                        service_url=service_url,
                        reason="unreachable",
                    )
                except Exception:
                    pass
        elif auth_rejected and not scopes_ok:
            # The server answered and rejected the key — definitive, and the
            # same signal the pre-recall authed probe used to provide, now from
            # a real request. The re-entry gate's authed probe lifts the state
            # once the key is fixed.
            write_connection_state("auth_failed", service_url, detail="401/403 during recall")
            clear_slow_streak(service_url)
        elif scopes_ok:
            # The server answered — an authenticated, real-workload proof of
            # life. Refresh the marker only when it isn't already fresh-ready,
            # so steady-state prompts don't rewrite the file every keystroke.
            clear_slow_streak(service_url)
            if not server_ready_hint(service_url):
                mark_server_ready(service_url)
            try:
                from _cognee_client import record_success as _breaker_success

                _breaker_success(service_url)
            except Exception:
                pass
        elif server_errors:
            # Reachable but failing (5xx on every answered scope, none ok):
            # record the state and, mirroring the explicit-search path, one
            # breaker failure for the prompt.
            write_connection_state("server_error", service_url, detail="5xx during recall")
            clear_slow_streak(service_url)
            try:
                from _cognee_client import record_failure as _breaker_failure

                _breaker_failure("http 5xx", service_url=service_url, reason="server_error")
            except Exception:
                pass
        elif scope_timeouts and not scopes_answered_err and not recall_slow:
            # Timeouts describe latency, not an outage: recall is never skipped,
            # only shortened (see recall_slow_until above). A timeout under the
            # short budget proves nothing new and does not extend it.
            streak = record_slow_probe(service_url)
            if streak >= slow_streak_threshold() or slow_until:
                mark_recall_slow(service_url)
                hook_log("slow_streak_escalated", {"base_url": service_url, "streak": streak})
    except Exception as exc:
        hook_log("recall_health_accounting_failed", {"error": str(exc)[:200]})

    # Bucket results by source for human-readable output. The server returns
    # plain dicts; anything else is skipped.
    by_source: dict[str, list] = {
        "session": [],
        "trace": [],
        "graph_context": [],
        "session_context": [],
        "code": [],
    }
    item_guidance = ""
    for r in results or []:
        if not isinstance(r, dict):
            continue
        # Work on a copy: the source is re-tagged and the text trimmed below,
        # and the caller's items are not ours to rewrite.
        r = copy.deepcopy(r)
        src = r.get("source", "session")
        # The graph scope tags results source=graph; keep the historical
        # graph_context bucket name so the status line, last_recall.json
        # consumers and the `g` counter stay stable.
        if src == "graph":
            r["source"] = "graph_context"
            src = "graph_context"
            # Preserve the own-session exclusion for structured source passages too.
            for item in evidence_items(r) or []:
                if (
                    session_id
                    and f"Session ID: {session_id}\n" in str(item.get("text") or "") + "\n"
                ):
                    item["status"] = "own_session"
            # Keep the retrieved context only (SDK-904): the question template
            # goes, own-session passages go, the rest is capped and pruned.
            # Unparsed text (a pre-1.6.0 bare context, an unknown template)
            # is injected whole — fail-open, logged.
            if r.get("text") and not any(render_item(i) for i in evidence_items(r) or []):
                trimmed, guidance, stats = trim_recall_text(
                    str(r.get("text") or ""), prompt, session_id
                )
                r["text"] = trimmed
                if guidance and not item_guidance:
                    item_guidance = guidance
                hook_log(
                    "recall_context_trimmed" if stats["parsed"] else "recall_context_unparsed",
                    stats,
                )
        if not _has_entry_content(r):
            continue
        by_source.setdefault(src, []).append(r)
    # A durable-preferences block can trail the graph item even without a
    # session id. The session_context request renders the same lines inside
    # its block, so it is only used when that request brought nothing.
    if item_guidance and not by_source.get("session_context"):
        by_source["session_context"].append({"source": "session_context", "content": item_guidance})

    counts = {k: len(v) for k, v in by_source.items()}
    total = sum(counts.values())

    # Session-cumulative counter, carried forward from this session's own
    # per-session marker: how many prompts this session has seen and on how
    # many of them memory actually injected something. This is the "memory
    # fired on 12 of 40 turns" activation number the status line shows next to
    # the per-turn count — no extra calls, just the turn that was already
    # counted. Keyed by host session, so /clear starts over and --resume
    # continues. Read here, before the dataset hint, which it gates.
    from pathlib import Path as _Path

    _state = _Path.home() / ".cognee-plugin" / "claude-code" / "last_recall.json"
    _key = get_session_key()
    _key_safe = bool(_key) and all(c.isalnum() or c in "._-" for c in _key)
    _per = _state.parent / "recall" / f"{_key}.json" if _key_safe else None
    _totals = {"turns": 0, "turns_with_hits": 0}
    if _per is not None and _per.exists():
        try:
            _prev = json.loads(_per.read_text(encoding="utf-8")).get("session_totals")
            if isinstance(_prev, dict):
                _totals["turns"] = max(0, int(_prev.get("turns", 0) or 0))
                _totals["turns_with_hits"] = max(0, int(_prev.get("turns_with_hits", 0) or 0))
        except Exception:
            pass
    _totals["turns"] += 1

    # Name the other datasets the user could search instead (see
    # _other_datasets_hint). The block is static text, so it rides along on
    # the session's FIRST answered prompt — the model keeps it from there —
    # and again on any prompt memory answered with nothing, the moment it is
    # actually needed (SDK-904). Not when nothing answered: the search it
    # recommends would fail the same way. Decoration on the recall path — it
    # must never break the hook output.
    dataset_hint = ""
    if scopes_ok and _dataset_hint_enabled() and (_totals["turns"] <= 1 or total == 0):
        try:
            dataset_hint = _other_datasets_hint(
                session_dataset,
                read_ids or ([write_id] if write_id else []),
                service_url,
                budget_deadline,
            )
        except Exception as exc:
            hook_log(
                "recall_error",
                {"scope": ["dataset_hint"], "error": str(exc)[:200], "verdict": "unknown"},
            )

    # Build a one-line visibility header so the user (via the assistant's
    # context) can tell that memory fired on this turn — both what it
    # recalled right now and what the previous turn persisted.
    header = (
        "Cognee memory: recall "
        f"{counts['graph_context']} memory"
        + (f" / {counts['code']} code" if code_lane else "")
        + "; "
        + saves_segment(saves_last_turn)
    )
    # Writes the server never received are not saves: a buffering outage gets
    # its own segment, plus whatever still waits for replay (SDK-467).
    for segment in buffered_saves_segments(saves_last_turn, warmup_backlog()):
        header += f"; {segment}"

    if scope_timeouts:
        header += "; memory recall timed out"
    # Tag each recalled item so the answer can credit it (SDK-1071).
    tags = (
        MemoryTags.load(_plugin_common._PLUGIN_DIR, get_session_key())
        if credit_enabled(_plugin_common.CITE_MEMORY_DEFAULT)
        else None
    )
    labels: dict = {}
    memory_sections = _injection_sections(by_source, tags, labels)
    # Reserve the largest possible drop notice before fitting. Replacing it
    # with the actual smaller count (or removing it) can only shrink output.
    entry_count = sum(len(section["entries"]) for section in memory_sections)
    reserved_drop = f"; {entry_count} dropped to fit" if entry_count else ""
    header += reserved_drop
    hint = _report_hint(prompt)
    hint_sections = [{"entries": [hint], "required": True, "counted": False}] if hint else []
    # The credit instruction goes right under the header, not after the memory:
    # Claude Code replaces hook context over 10,000 characters with a file and a
    # 2 KB preview, so anything at the end could be cut off. Reserved with the
    # largest count and corrected after fitting, which can only shrink it; never
    # deduplicated, since the count changes the meaning. It outranks every memory
    # entry but is not required: at the smallest budget it gives way rather than
    # fail the whole recall (SDK-1071).
    first_credit = bool(labels) and not has_credited(_plugin_common._PLUGIN_DIR, _key)
    reserved_instruction = (
        credit_instruction(first_credit, len(set(labels.values()))) if labels else ""
    )
    instruction_sections = (
        [{"entries": [reserved_instruction], "priority": 100, "counted": False, "dedupe": False}]
        if reserved_instruction
        else []
    )
    sections = (
        [{"entries": [header], "required": True}]
        + instruction_sections
        + hint_sections
        + memory_sections
    )
    if not total and not scope_timeouts:
        sections.append(
            {"entries": ["(no memory matches for this prompt)"], "priority": 1, "counted": False}
        )
    if dataset_hint:
        sections.append({"entries": [dataset_hint], "priority": 0, "counted": False})
    notice = _bounded_notice(header, notice)

    def measure(text):
        return len(json.dumps(_recall_output(text, notice))) + 1

    if cloud_enabled(runtime) and _key_safe and total:
        from _injection_ledger import render as render_deduped

        try:
            full_context, injection_stats, header = render_deduped(
                _key,
                header,
                sections[1:],
                injection_limit(),
                measure=measure,
            )
        except (OSError, ValueError) as exc:
            hook_log("injection_ledger_failed", {"error": str(exc)[:200]})
            full_context, injection_stats = fit_sections(
                sections, injection_limit(), measure=measure
            )
    else:
        full_context, injection_stats = fit_sections(sections, injection_limit(), measure=measure)
    if _key_safe:
        try:
            unavailable = [
                item
                for entries in by_source.values()
                for entry in entries
                for item in evidence_items(entry) or []
                if item.get("status") != "available"
            ]
            write_receipt(_key, memory_sections, injection_stats, unavailable)
        except (OSError, ValueError) as exc:
            hook_log("injection_receipt_failed", {"error": str(exc)[:200]})
    retrieved_counts = counts
    retrieved_total = total
    counts = {source: injection_stats["source_hits"].get(source, 0) for source in counts}
    total = sum(counts.values())
    dropped = injection_stats["entries_dropped"]
    actual_header = full_context.split("\n", 1)[0]
    actual_header = actual_header.replace(
        f"recall {retrieved_counts['graph_context']} memory",
        f"recall {counts['graph_context']} memory",
        1,
    )
    if code_lane:
        actual_header = actual_header.replace(
            f" / {retrieved_counts['code']} code", f" / {counts['code']} code", 1
        )
    if reserved_drop:
        actual_header = actual_header.replace(
            reserved_drop, f"; {dropped} dropped to fit" if dropped else "", 1
        )
    full_context = actual_header + full_context[len(full_context.split("\n", 1)[0]) :]
    if tags is not None:
        # Only what survived the budget and the dedupe was injected on this turn.
        for mark, tag in labels.items():
            if mark in full_context:
                tags.show(tag)
    if reserved_instruction and reserved_instruction in full_context:
        if tags.found:
            instruction = credit_instruction(first_credit, len(tags.found))
            full_context = full_context.replace(reserved_instruction, instruction, 1)
        else:
            full_context = full_context.replace("\n\n" + reserved_instruction, "", 1)
    injection_stats["context_chars"] = len(full_context)
    if total:
        _totals["turns_with_hits"] += 1

    # Write last-turn counts so the status line script can render them.
    # Best-effort; failure here must not break the hook output.
    try:
        _state.parent.mkdir(parents=True, exist_ok=True)
        _payload = json.dumps(
            {
                "session_id": session_id,
                # Host session key too: the marker is per-integration, so the
                # status line needs this to tell "my counts" from another live
                # session's before rendering them.
                "session_key": _key,
                "ts": __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .isoformat(timespec="seconds"),
                "hits": counts,
                "retrieved_hits": retrieved_counts,
                "entries_dropped": dropped,
                "per_scope": per_scope,
                "saves_last_turn": saves_last_turn,
                "session_totals": _totals,
            }
        )
        # Machine-wide copy: kept because cognee_plugin.py resolves the active
        # session id from it.
        _state.write_text(_payload, encoding="utf-8")
        # Per-session copy, which is what the status line reads: with several
        # terminals open the single shared file only ever holds the counts of
        # whoever prompted last, so every other bar would show nothing (or, worse,
        # a neighbour's numbers).
        if _per is not None:
            _per.parent.mkdir(parents=True, exist_ok=True)
            _per.write_text(_payload, encoding="utf-8")
    except Exception as exc:
        hook_log("last_recall_write_failed", {"error": str(exc)[:200]})

    hook_log(
        "context_lookup_hit" if total else "context_lookup_empty",
        {
            "counts": counts,
            "retrieved_hits": retrieved_total,
            "per_scope": per_scope,
            "saves_last_turn": saves_last_turn,
            "elapsed_ms": elapsed_ms(recall_start),
            **{
                k: v
                for k, v in injection_stats.items()
                if k not in {"entries", "source_hits", "deduped_entries"}
            },
        },
    )
    message = (
        f"injected context ({counts})"
        if total
        else "no recall matches"
        if not retrieved_total
        else "no new memory injected"
    )
    if dropped:
        message += f"; {dropped} dropped to fit"
    notify(f"{message}; saves last turn {saves_last_turn}")

    from _session_stats import update as update_stats

    update_stats(
        _key,
        recalls=1,
        hits=int(total > 0),
        entries_injected=injection_stats["entries_kept"],
        entries_deduped=injection_stats.get("entries_deduped", 0),
        chars_injected=len(full_context),
        dedup_chars_saved=injection_stats.get("dedup_chars_saved", 0),
        dedup_measurements=int("dedup_chars_saved" in injection_stats),
        recall_ms=elapsed_ms(recall_start),
        metadata={"dataset": session_dataset, "session_id": session_id, "server_url": service_url},
    )

    # Audit log: persist full recall details per turn. The hook output stays a
    # short summary because Codex renders additionalContext in the terminal.
    try:
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        from pathlib import Path as _Path

        from _logfiles import append_line as _append_log_line

        _audit = _Path.home() / ".cognee-plugin" / "claude-code" / "recall-audit.log"
        # Full prompt + full injected context per line averaged ~9 KB a turn and
        # made this the fastest-growing file in the state dir. The audit is for
        # seeing *what* was recalled, which the head of each field shows; the
        # complete context still reaches the model via additionalContext.
        _append_log_line(
            _audit,
            json.dumps(
                {
                    "ts": _dt.now(_tz.utc).isoformat(timespec="seconds"),
                    "session_id": session_id,
                    "prompt": _audit_clip(prompt, _AUDIT_PROMPT_CHARS),
                    "hits": counts,
                    "retrieved_hits": retrieved_counts,
                    "per_scope": per_scope,
                    # The header the user sees: an empty recall no longer
                    # puts it in the injected context (SDK-1071).
                    "header": header,
                    "context": _audit_clip(full_context, _AUDIT_CONTEXT_CHARS),
                    **{k: v for k, v in injection_stats.items() if k != "entries"},
                }
            ),
        )
    except Exception as exc:
        hook_log("recall_audit_write_failed", {"error": str(exc)[:200]})

    # The Stop hook checks the answer's citations against these (SDK-1071).
    if tags is not None:
        try:
            tags.save(_plugin_common._PLUGIN_DIR, get_session_key())
        except Exception as exc:
            hook_log("memory_tags_write_failed", {"error": str(exc)[:200]})

    return _recall_output(full_context, notice), total


def _recall_min_prompt_chars() -> int:
    """Prompts shorter than this skip recall (capture keeps its own 5-char floor).

    COGNEE_RECALL_MIN_PROMPT_CHARS lets a host raise the floor so acknowledgements
    and one-word nudges do not cost a lookup and an injected context block. The
    default matches the stock gate, so an unset variable changes nothing.
    """
    raw = os.environ.get("COGNEE_RECALL_MIN_PROMPT_CHARS", "").strip()
    try:
        return max(5, int(raw)) if raw else 5
    except ValueError:
        return 5


def main():
    if is_observer_child():
        return
    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return

    session_key_candidate, session_key_source = resolve_session_key_from_payload(payload)
    if session_key_candidate:
        set_session_key(session_key_candidate)
    hook_log(
        "context_lookup_session_key", {"source": session_key_source, "value": session_key_candidate}
    )
    if not get_session_key():
        hook_log("context_lookup_missing_session_key")
        return

    prompt = payload.get("prompt", "")
    if not prompt or len(prompt) < 5:
        return
    # Only a raised floor applies here: the stock gate above keeps its exact
    # behavior (whitespace counts), so an unset variable changes nothing.
    min_chars = _recall_min_prompt_chars()
    if min_chars > 5 and len(prompt.strip()) < min_chars:
        hook_log("context_lookup_short_prompt", {"chars": len(prompt.strip()), "min": min_chars})
        return
    cwd = str(payload.get("cwd") or "") or os.getcwd()

    notice_output = with_base_url_notice(None, "UserPromptSubmit") or {}
    notice = str(notice_output.get("hookSpecificOutput", {}).get("additionalContext") or "")
    output = None
    try:
        with quiet_hook_output("session-context-lookup"):
            output = asyncio.run(_run(prompt, cwd, notice=notice))
    except Exception as exc:
        hook_log("context_lookup_exception", {"error": str(exc)[:200]})
        phase_end("recalling", count=0, ok=False)
    if output is None and notice:
        header = "Cognee memory: recall unavailable"
        output = _recall_output(header, _bounded_notice(header, notice))
    if output:
        print(json.dumps(output))


if __name__ == "__main__":
    main()
