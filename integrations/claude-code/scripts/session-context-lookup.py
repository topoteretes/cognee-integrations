#!/usr/bin/env python3
"""Search session + trace + agent guidance + graph for context relevant to the user's prompt.

Runs on the UserPromptSubmit hook. Calls the server's ``/api/v1/recall``
once per scope (``session``, ``trace``, ``session_context``, ``graph``), all
dispatched concurrently, so every layer the SessionManager holds (QA
entries, agent trace steps, standing agent guidance, and the graph knowledge
built by ``improve()``) flows back into the agent's context.

Configuration:
    Resolves session state via Cognee HTTP endpoints.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    DEFINITIVE_FAILURE_STATES,
    _float_env,
    authed_liveness,
    buffered_saves_segments,
    clear_slow_streak,
    elapsed_ms,
    get_session_key,
    hook_log,
    load_resolved,
    mark_server_ready,
    notify,
    outage_header,
    probe_health,
    quiet_hook_output,
    read_and_reset_save_counter,
    read_connection_state,
    recall_via_http,
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
    write_connection_state,
)
from _recall_http import DOWN, SLOW, classify_transport_exception
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
TRUNCATE_GRAPH_CTX = 1500
# Smallest deadline worth dispatching; with less budget than this, nothing is
# fired rather than sending every scope a doomed request.
MIN_SCOPE_TIMEOUT = 0.2


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
        # graph_context entries carry `content`; graph_completion results
        # (folded in from scope=graph) carry `text`. Try both.
        content = str(entry.get("content", "") or entry.get("text", ""))[:TRUNCATE_GRAPH_CTX]
        return f"[graph-snapshot]\n{content}"

    if source == "session_context":
        content = str(entry.get("content", "") or entry.get("text", ""))[:TRUNCATE_GRAPH_CTX]
        return f"[agent-guidance]\n{content}"

    if source == "code":
        # Deterministic code-graph facts (ResponseCodeEntry): `text` is the
        # normalized renderable field; raw payloads keep full structure.
        content = str(entry.get("text", "") or entry.get("content", ""))[:TRUNCATE_GRAPH_CTX]
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
        if feedback:
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


def _count_cross_session_hits(by_source: dict, session_id: str) -> int:
    """How many injected results came from outside this session.

    The session, trace and agent-guidance scopes are queried by ``session_id``,
    so everything they return is this session's own. Only the knowledge graph
    reaches across sessions: the bridge stamps every synced session document
    with a ``Session ID: <id>`` header (and distilled learnings keep the id in
    their heading), so a graph passage that does not mention the current id
    came from an earlier session — or from a ``remember``-ed document, which is
    knowledge this conversation never produced either. That is the number the
    status line shows as ``N from past sessions``: what memory contributed that
    Claude could not have known from this conversation alone.
    """
    count = 0
    for entry in by_source.get("graph_context") or []:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("content", "") or entry.get("text", "") or "")
        if not session_id or session_id not in text:
            count += 1
    return count


def _outage_output(state: str) -> dict:
    """Envelope for a prompt whose recall was skipped: see ``outage_header``."""
    session_id = _load_session_id()
    saves = read_and_reset_save_counter(session_id) if session_id else {}
    header = outage_header(state, saves, warmup_backlog(), "; ")
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": header,
            "systemMessage": header,
        }
    }


def _has_entry_content(entry: dict) -> bool:
    """Return True when a recall entry has useful content to inject."""
    source = entry.get("source", "")
    if source == "graph_context":
        return bool(str(entry.get("content", "") or entry.get("text", "")).strip())
    if source == "session_context":
        return bool(str(entry.get("content", "") or entry.get("text", "")).strip())
    if source == "code":
        return bool(str(entry.get("text", "") or entry.get("content", "")).strip())
    if source == "trace":
        fields = ("origin_function", "status", "session_feedback", "method_return_value")
    else:
        fields = ("question", "answer")
    return any(str(entry.get(field, "") or "").strip() for field in fields)


async def _run(prompt: str, cwd: str = "") -> dict | None:
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
    probe_timeout = _float_env("COGNEE_READY_PROBE_TIMEOUT", 1.0)
    prior = read_connection_state()
    # Permissive on purpose: "same target" unless the two URLs provably differ,
    # so a recorded state still applies when a URL is unknown. Mirrors the
    # renderer's _url_mismatch (equivalence pinned by
    # tests/test_connection_target_match.py).
    prior_same_target = same_connection_target(service_url, str(prior.get("base_url") or ""))
    prior_state = str(prior.get("state") or ("ready" if prior.get("ready_at") else ""))
    known_bad = prior_same_target and prior_state in (
        "auth_failed",
        "unreachable",
        "server_error",
        "not_responding",
    )
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
            return _outage_output(state if state in DEFINITIVE_FAILURE_STATES else prior_state)

    session_id = _load_session_id()
    if not session_id:
        hook_log("no_session_id", {"event": "context_lookup"})
        return None

    # NOTE: the warmup-buffer drain deliberately does NOT run here. This hook
    # is synchronous on the keystroke->answer path, and replaying N buffered
    # entries (~1s of server work each) stalled the prompt for 10-30s after any
    # long turn (#298). The async sibling hook on this same event
    # (store-user-prompt.py) drains instead; improve/SessionEnd re-drain too.
    saves_last_turn = read_and_reset_save_counter(session_id)

    # Run scopes independently AND concurrently. Independently: a failure in
    # one (e.g. graph search hitting an empty/locked Ladybug DB) must not
    # discard hits from the others — the server's recall loops over scopes
    # and fails the whole request on the first failure, so every scope is its
    # own request. Concurrently: the scopes share nothing (the session layers
    # read the server's session cache, graph and code hit the graph store),
    # so all of them are dispatched at once and the prompt waits for the
    # slowest one instead of the sum. Sequentially, every cheap scope cost a
    # full round trip on top of the graph search — three of them on a cloud
    # server — and the code lane, when armed, could burn seconds before graph
    # even started. Every result still lands in the same injected context.
    results: list = []
    # A single graph scope on purpose: the server (cognee >= 1.4) aliases the
    # old graph_context scope to graph, so a graph_context + graph pair ran
    # the same full graph retrieval twice per prompt. HYBRID_COMPLETION
    # combines BM25 + vector + graph retrieval (with only_context=True the LLM
    # completion is skipped server-side either way). The list order is the
    # canonical reporting order (per_scope, logs), not a dispatch order.
    scope_specs = [
        (["session"], None, None),
        (["trace"], None, None),
        (["session_context"], None, "agent"),
        (["graph"], "HYBRID_COMPLETION", None),
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
        scope_specs.insert(3, (["code"], None, None))
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
    # The default (12s) is sized for the graph scope, the only expensive call.
    # Graph search time grows with the dataset and with the round trip to a
    # remote (cloud) server, so a cap tuned for a small local graph silently
    # drops graph memory once either grows.
    recall_budget = _float_env("COGNEE_RECALL_BUDGET", 12.0)
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
        part, exc = None, None
        t0 = time.monotonic()
        try:
            # recall_via_http blocks on urllib (and bounds itself with a
            # daemon thread), so it runs in a worker; the name is resolved at
            # call time so the module attribute stays the seam.
            part = await asyncio.to_thread(
                recall_via_http,
                prompt,
                session_id=session_id,
                top_k=TOP_K,
                scope=scope_list,
                only_context=True,
                search_type=qtype,
                context_profile=context_profile,
                dataset=scope_dataset,
                dataset_ids=scope_dataset_ids,
                code_query=scope_code_query,
                timeout=scope_timeout,
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
        if isinstance(exc, _urlerr.HTTPError) and exc.code == 404 and scope_list == ["graph"]:
            # A dataset nobody has written to yet has no graph, and the
            # server answers the graph scope with 404 (DatasetNotFound)
            # until the first cognify lands. On a fresh install that is
            # every prompt of the first session — expected, not an error:
            # keep it out of recall_error and the health accounting
            # (scopes_answered_err) so real failures stay visible (SDK-469).
            hook_log("recall_graph_not_built", {"scope": scope_list, "dataset": scope_dataset})
            continue
        if isinstance(exc, _urlerr.HTTPError):
            scopes_answered_err += 1
            if exc.code in (401, 403):
                auth_rejected = True
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
        elif scope_timeouts and not scopes_answered_err:
            # Every attempted scope timed out and none got an HTTP answer: no
            # verdict on its own, but N consecutive such prompts are a pattern.
            # Escalate to "not_responding" — deliberately distinct from
            # "unreachable" (positively absent: refused/DNS): the server exists
            # but is not answering. A lone timeout never writes anything.
            streak = record_slow_probe(service_url)
            if streak >= slow_streak_threshold():
                write_connection_state(
                    "not_responding",
                    service_url,
                    detail="%d consecutive timeout-only prompts" % streak,
                )
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
    for r in results or []:
        if not isinstance(r, dict):
            continue
        src = r.get("source", "session")
        # The graph scope tags results source=graph; keep the historical
        # graph_context bucket name so the status line, last_recall.json
        # consumers and the `g` counter stay stable.
        if src == "graph":
            r["source"] = "graph_context"
            src = "graph_context"
        if not _has_entry_content(r):
            continue
        by_source.setdefault(src, []).append(r)

    counts = {k: len(v) for k, v in by_source.items()}
    total = sum(counts.values())
    cross_session_hits = _count_cross_session_hits(by_source, session_id)

    # Write last-turn counts so the status line script can render them.
    # Best-effort; failure here must not break the hook output.
    try:
        from pathlib import Path as _Path

        _state = _Path.home() / ".cognee-plugin" / "claude-code" / "last_recall.json"
        _state.parent.mkdir(parents=True, exist_ok=True)
        _key = get_session_key()
        _key_safe = bool(_key) and all(c.isalnum() or c in "._-" for c in _key)
        _per = _state.parent / "recall" / f"{_key}.json" if _key_safe else None
        # Session-cumulative counter, carried forward from this session's own
        # per-session marker: how many prompts this session has seen and on how
        # many of them memory actually injected something. This is the
        # "memory fired on 12 of 40 turns" activation number the status line
        # shows next to the per-turn count — no extra calls, just the turn that
        # was already counted. Keyed by host session, so /clear starts over and
        # --resume continues.
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
        if total > 0:
            _totals["turns_with_hits"] += 1
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
                "cross_session_hits": cross_session_hits,
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

    # Build a one-line visibility header so the user (via the assistant's
    # context) can tell that memory fired on this turn — both what it
    # recalled right now and what the previous turn persisted.
    header = (
        "Cognee memory: recall "
        f"{counts['session']} session / {counts['trace']} trace / "
        f"{counts['graph_context']} graph / {counts['session_context']} agent"
        + (f" / {counts['code']} code" if code_lane else "")
        + "; "
        + saves_segment(saves_last_turn)
    )
    # Writes the server never received are not saves: a buffering outage gets
    # its own segment, plus whatever still waits for replay (SDK-467).
    for segment in buffered_saves_segments(saves_last_turn, warmup_backlog()):
        header += f"; {segment}"

    section_lines = []
    if by_source.get("session_context"):
        section_lines.append("=== Active agent guidance ===")
        for e in by_source["session_context"]:
            section_lines.append(_format_entry(e))
            section_lines.append("")
    if by_source.get("code"):
        section_lines.append("=== Code graph facts ===")
        for e in by_source["code"]:
            section_lines.append(_format_entry(e))
            section_lines.append("")
    if by_source.get("graph_context"):
        section_lines.append("=== Knowledge graph snapshot ===")
        for e in by_source["graph_context"]:
            section_lines.append(_format_entry(e))
            section_lines.append("")
    if by_source.get("trace"):
        section_lines.append("=== Prior agent trace ===")
        for e in by_source["trace"]:
            section_lines.append(_format_entry(e))
            section_lines.append("")
    if by_source.get("session"):
        section_lines.append("=== Prior session turns ===")
        for e in by_source["session"]:
            section_lines.append(_format_entry(e))
            section_lines.append("")

    if total > 0:
        full_context = (
            f"{header}\n\nRelevant context from this session's memory:\n\n"
            + "\n".join(section_lines).strip()
        )
        hook_log(
            "context_lookup_hit",
            {
                "counts": counts,
                "cross_session_hits": cross_session_hits,
                "per_scope": per_scope,
                "saves_last_turn": saves_last_turn,
                "elapsed_ms": elapsed_ms(recall_start),
            },
        )
        notify(f"injected context ({counts}); saves last turn {saves_last_turn}")
    else:
        full_context = f"{header}\n\n(no memory matches for this prompt)"
        hook_log(
            "context_lookup_empty",
            {
                "per_scope": per_scope,
                "saves_last_turn": saves_last_turn,
                "elapsed_ms": elapsed_ms(recall_start),
            },
        )
        notify(f"no recall matches; saves last turn {saves_last_turn}")

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
                    "per_scope": per_scope,
                    "context": _audit_clip(full_context, _AUDIT_CONTEXT_CHARS),
                }
            ),
        )
    except Exception as exc:
        hook_log("recall_audit_write_failed", {"error": str(exc)[:200]})

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": full_context,
            "systemMessage": header,
        }
    }
    return output


def main():
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
    cwd = str(payload.get("cwd") or "") or os.getcwd()

    output = None
    try:
        with quiet_hook_output("session-context-lookup"):
            output = asyncio.run(_run(prompt, cwd))
    except Exception as exc:
        hook_log("context_lookup_exception", {"error": str(exc)[:200]})
    if output:
        print(json.dumps(output))


if __name__ == "__main__":
    main()
