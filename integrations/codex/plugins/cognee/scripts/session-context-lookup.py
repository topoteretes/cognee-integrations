#!/usr/bin/env python3
"""Search session + trace + agent guidance + graph for context relevant to the user's prompt.

Runs on the Codex UserPromptSubmit hook. Calls ``cognee.recall`` once per
scope (``session``, ``trace``, ``session_context``, ``graph``) so every
layer the SessionManager holds (QA entries, agent trace steps, standing
agent guidance, and the graph knowledge built by ``improve()``) flows back
into Codex's context.

Configuration:
    Resolves session state via Cognee HTTP endpoints.
"""

import asyncio
import json
import os
import sys
import time

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    authed_liveness,
    bounded_dim_mismatch_hint,
    clear_slow_streak,
    get_session_key,
    hook_log,
    load_resolved,
    mark_server_ready,
    notify,
    probe_health,
    quiet_hook_output,
    read_and_reset_save_counter,
    read_connection_state,
    recall_via_http,
    record_slow_probe,
    resolve_runtime_mode,
    resolve_session_key_from_payload,
    resolve_user,
    same_connection_target,
    server_ready_hint,
    service_url_is_local,
    set_session_key,
    slow_streak_threshold,
    write_connection_state,
)
from _recall_http import DOWN, SLOW, classify_transport_exception
from cognee_statusline_render import render_status_for_host
from config import ensure_cognee_ready, get_dataset, get_session_id, load_config

#: Per-field caps for recall-audit.log lines (characters).
_AUDIT_PROMPT_CHARS = 2000
_AUDIT_CONTEXT_CHARS = 4000


def _audit_clip(value, limit: int) -> str:
    """Head of ``value`` for the audit log, marked when clipped."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[+{len(text) - limit} chars]"



def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


TOP_K = 5
TRUNCATE_ANSWER = 500
TRUNCATE_RETURN = 400
TRUNCATE_GRAPH_CTX = 1500
RECENT_TRACE_FALLBACK_TOP_K = 5
# Smallest per-scope timeout worth dispatching; with less budget than this
# left, remaining scopes are skipped rather than fired with a doomed deadline.
MIN_SCOPE_TIMEOUT = 0.2


def _load_session_id() -> str:
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    if not session_id:
        config = load_config()
        session_id = get_session_id(config)
    return session_id


def _load_user_id() -> str:
    return load_resolved().get("user_id", "")


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


async def _recent_trace_fallback(session_id: str, user_id: str, top_k: int) -> list[dict]:
    """Return recent trace rows directly when semantic trace recall misses.

    Tool calls are chronological session context, not only semantic context. A
    casual next prompt often will not match the words in a tool output, but the
    agent still needs to see the recent tool calls it just made.
    """
    try:
        from cognee.infrastructure.session.get_session_manager import get_session_manager

        sm = get_session_manager()
        if not sm.is_available or not user_id:
            return []
        raw_trace = await sm.get_agent_trace_session(user_id=user_id, session_id=session_id)
        entries = list(raw_trace or [])[-top_k:]
    except Exception as exc:
        hook_log("trace_fallback_error", {"error": str(exc)[:200]})
        return []

    normalized: list[dict] = []
    for entry in entries:
        if hasattr(entry, "model_dump"):
            entry = entry.model_dump()
        elif hasattr(entry, "dict"):
            entry = entry.dict()
        elif hasattr(entry, "__dict__"):
            entry = dict(entry.__dict__)
        if not isinstance(entry, dict):
            continue
        entry["source"] = "trace"
        if _has_entry_content(entry):
            normalized.append(entry)
    return normalized


async def _run(prompt: str, cwd: str = "") -> dict | None:
    config = load_config()
    runtime = resolve_runtime_mode()
    cloud_mode = runtime["mode"] == "http"
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
            if state in ("auth_failed", "unreachable", "server_error"):
                # A definitive verdict: refresh/replace the recorded failure.
                write_connection_state(state, service_url, detail="authed liveness probe")
                clear_slow_streak(service_url)
            # "slow"/"unknown" from the probe is NO verdict — keep the recorded
            # state untouched rather than promote a timeout to a failure.
            hook_log("recall_skipped_not_ready", {"base_url": service_url, "state": state})
            return None

    if not cloud_mode:
        await ensure_cognee_ready(config)

    session_id = _load_session_id()
    if not session_id:
        hook_log("no_session_id", {"event": "context_lookup"})
        return None

    # NOTE: the warmup-buffer drain deliberately does NOT run here. This hook
    # is synchronous on the keystroke->answer path, and replaying N buffered
    # entries (~1s of server work each) stalled the prompt for 10-30s after any
    # long turn (#298). The sibling hook on this same event
    # (store-user-prompt.py) drains instead; improve/SessionEnd re-drain too.
    saves_last_turn = read_and_reset_save_counter(session_id)

    # Run scopes independently: a failure in one (e.g. graph search hitting an
    # empty/locked Ladybug DB) must not discard hits already collected from the
    # others. cognee.recall loops over scopes and re-raises on the first failure,
    # so we call it once per scope and collect whatever succeeds.
    results: list = []
    # Cheap scopes first (tens of ms each), the graph search last: it is the
    # only call that can consume a full per-call timeout, and running it
    # earlier starved session_context out of the budget entirely. A single
    # graph scope on purpose: the server (cognee >= 1.4) aliases the old
    # graph_context scope to graph, so a graph_context + graph pair ran the
    # same full graph retrieval twice per prompt. HYBRID_COMPLETION combines
    # BM25 + vector + graph retrieval (with only_context=True the LLM
    # completion is skipped server-side either way).
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
    # here. Placed before graph: the code lane is the cheapest call when its
    # snapshot is warm, and graph is the long pole that must stay last.
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
    if not cloud_mode:
        import cognee
        from cognee.modules.search.types import SearchType

        user = await resolve_user(_load_user_id())

    # Per-scope instrumentation (WS7 observability): capture {hits, elapsed_ms}
    # for every scope, keyed by its stable label. Pre-seed all scopes as
    # skipped, in canonical order and before the breaker-open branch below can
    # blank scope_specs, so the event always carries the full set; the loop
    # overwrites each scope that actually runs. Purely additive: it must not
    # touch recall results, ordering, or control flow, and must never raise into
    # the keystroke->answer path.
    per_scope: dict[str, dict] = {
        scope_list[0]: {"hits": 0, "elapsed_ms": 0, "skipped": True}
        for scope_list, _qtype, _profile in scope_specs
    }

    # Hard time-box: this hook is on the keystroke->answer path, so recall must
    # never be the long pole. Each scope gets a short per-call timeout, and the
    # whole loop stops once the overall budget is spent. Partial results are fine.
    recall_timeout = _float_env("COGNEE_RECALL_TIMEOUT", 2.5)
    budget_deadline = time.monotonic() + _float_env("COGNEE_RECALL_BUDGET", 4.0)
    # Respect the shared circuit breaker: when the server has been failing (tripped
    # by the explicit recall path), skip this per-prompt recall rather than hammering
    # a down backend on every keystroke. HTTP/cloud mode only.
    if cloud_mode:
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
    for scope_list, qtype, context_profile in scope_specs:
        # Clamp each call to what is left of the budget so a single scope can
        # never overshoot the deadline (previously a scope dispatched just
        # before the deadline could run a full recall_timeout past it). Below
        # the floor a call cannot return anything useful, so skip the
        # remaining scopes instead of firing a doomed request.
        remaining = budget_deadline - time.monotonic()
        if remaining < MIN_SCOPE_TIMEOUT:
            hook_log("recall_budget_exceeded", {"collected": len(results)})
            break
        scope_timeout = min(recall_timeout, remaining)
        # The code lane searches the indexed repo's own (narrow) dataset with
        # a structured query; every other scope keeps the session dataset.
        is_code_scope = bool(code_lane) and scope_list == ["code"]
        scope_dataset = code_lane["dataset"] if is_code_scope else get_dataset(config)
        scope_code_query = code_lane["code_query"] if is_code_scope else None
        part = None
        t0 = time.monotonic()
        try:
            if cloud_mode:
                part = recall_via_http(
                    prompt,
                    session_id=session_id,
                    top_k=TOP_K,
                    scope=scope_list,
                    only_context=True,
                    search_type=qtype,
                    context_profile=context_profile,
                    dataset=scope_dataset,
                    code_query=scope_code_query,
                    timeout=scope_timeout,
                )
            else:
                query_type = getattr(SearchType, qtype, None) if qtype else None
                part = await asyncio.wait_for(
                    cognee.recall(
                        prompt,
                        session_id=session_id,
                        top_k=TOP_K,
                        scope=scope_list,
                        only_context=True,
                        query_type=query_type,
                        user=user,
                        **({"context_profile": context_profile} if context_profile else {}),
                        **(
                            {"datasets": [scope_dataset], "code_query": scope_code_query}
                            if is_code_scope
                            else {}
                        ),
                    ),
                    timeout=scope_timeout,
                )
            if part:
                results.extend(part)
            scopes_ok += 1
        except Exception as exc:
            import urllib.error as _urlerr

            if isinstance(exc, asyncio.TimeoutError):
                verdict = SLOW  # pre-3.11 asyncio.TimeoutError isn't TimeoutError
            else:
                verdict = classify_transport_exception(exc)
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
        finally:
            # hits = raw count from this scope's call (pre-bucketing/filtering);
            # elapsed_ms measured around the call, recorded even when it errored.
            per_scope[scope_list[0]] = {
                "hits": len(part or []),
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            }
        if server_down:
            # Positively absent (refused/DNS): the remaining scopes would fail
            # the same way in milliseconds each — stop here.
            hook_log("recall_server_down", {"base_url": service_url})
            break
        if auth_rejected:
            # Every scope shares the same API key, so the remaining scopes are
            # doomed to the same 401/403 — don't spend the budget on them.
            hook_log("recall_auth_rejected", {"base_url": service_url})
            break

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
                if cloud_mode:
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
            if cloud_mode:
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
            if cloud_mode:
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

    # Bucket results by _source for human-readable output.
    # Local SDK mode returns Pydantic models (ResponseQAEntry, etc.); cloud
    # mode returns plain dicts via HTTP. Normalize to dicts here.
    by_source: dict[str, list] = {
        "session": [],
        "trace": [],
        "graph_context": [],
        "session_context": [],
        "code": [],
    }
    for r in results or []:
        if hasattr(r, "model_dump"):
            r = r.model_dump()
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

    if not cloud_mode and not by_source.get("trace"):
        fallback_traces = await _recent_trace_fallback(
            session_id,
            _load_user_id(),
            RECENT_TRACE_FALLBACK_TOP_K,
        )
        if fallback_traces:
            by_source["trace"].extend(fallback_traces)
            hook_log("trace_fallback_hit", {"count": len(fallback_traces)})

    counts = {k: len(v) for k, v in by_source.items()}
    total = sum(counts.values())

    # Write last-turn counts so the status line script can render them.
    # Best-effort; failure here must not break the hook output.
    try:
        from pathlib import Path as _Path

        _state = _Path.home() / ".cognee-plugin" / "codex" / "last_recall.json"
        _state.parent.mkdir(parents=True, exist_ok=True)
        _state.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "ts": __import__("datetime")
                    .datetime.now(__import__("datetime").timezone.utc)
                    .isoformat(timespec="seconds"),
                    "hits": counts,
                    "per_scope": per_scope,
                    "saves_last_turn": saves_last_turn,
                }
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        hook_log("last_recall_write_failed", {"error": str(exc)[:200]})

    # Build a visibility header so the user (via the assistant's
    # context) can tell that memory fired on this turn — both what it
    # recalled right now and what the previous turn persisted.
    status_line = render_status_for_host(get_session_key())
    header = (
        f"{status_line}\n"
        "Cognee memory: recall "
        f"{counts['session']} session / {counts['trace']} trace / "
        f"{counts['graph_context']} graph / {counts['session_context']} agent"
        + (f" / {counts['code']} code" if code_lane else "")
        + "; saved last turn "
        f"{saves_last_turn['prompt']} prompt / {saves_last_turn['trace']} trace / "
        f"{saves_last_turn['answer']} answer"
    )

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
            {"counts": counts, "per_scope": per_scope, "saves_last_turn": saves_last_turn},
        )
        notify(f"injected context ({counts}); saves last turn {saves_last_turn}")
    else:
        # Zero results can mean a genuine miss OR that the embedding model changed
        # since indexing (stored vs query vectors differ in size, so nothing can
        # match). Only the local store is introspectable here; surface a one-line
        # actionable error when a mismatch is positively confirmed, else fall back
        # to the normal "no matches" line.
        dim_message = None
        if service_url_is_local(service_url):
            try:
                dim_message = await bounded_dim_mismatch_hint(timeout=2.0)
            except Exception as exc:
                hook_log("dim_check_error", {"error": str(exc)[:200]})
        if dim_message:
            full_context = f"{header}\n\n{dim_message}"
            hook_log("context_lookup_dim_mismatch", {"message": dim_message})
            notify(dim_message)
        else:
            full_context = f"{header}\n\n(no memory matches for this prompt)"
            hook_log(
                "context_lookup_empty",
                {"per_scope": per_scope, "saves_last_turn": saves_last_turn},
            )
            notify(f"no recall matches; saves last turn {saves_last_turn}")

    # Audit log: persist full recall details per turn for debugging.
    try:
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        from pathlib import Path as _Path

        from _logfiles import append_line as _append_log_line

        _audit = _Path.home() / ".cognee-plugin" / "codex" / "recall-audit.log"
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

    # additionalContext is the MODEL injection channel and must carry the full
    # recalled content — trimming it to the status line would silently disable
    # memory for the model. systemMessage carries the short status for the
    # terminal (mirrors the claude-code integration); hosts that render
    # additionalContext directly will still show the full block.
    output = {
        "systemMessage": header,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": full_context,
        },
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
    return output


if __name__ == "__main__":
    print(
        json.dumps(
            main()
            or {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "",
                }
            }
        )
    )
