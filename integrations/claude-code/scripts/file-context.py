#!/usr/bin/env python3
"""PreToolUse(Read) hook: file-scoped memory for the file about to be read.

Fires on Claude Code's ``Read`` tool and returns ``additionalContext`` — what the
code graph knows about that file (symbols with line numbers, what they call,
imports) and, optionally, what the knowledge graph remembers about it. The
model sees the map before the territory: it can jump to the right line
instead of scrolling, and it knows which other files the read one leans on.

Purely additive: never blocks or modifies the read (no ``permissionDecision``),
never raises, and prints nothing when there is nothing to say. Bounded by
``COGNEE_FILE_CONTEXT_BUDGET`` (seconds) so a slow server cannot stall the
read; a Read the model does dozens of times per turn cannot afford more.

Gates, in order (each logs ``file_context_skipped`` with its reason):

* off — ``COGNEE_FILE_CONTEXT`` is false;
* not a file path (``Read`` on a notebook cell, a directory, an image);
* sensitive path (``.env``, keys, credentials — the capture deny list);
* seen — the same session already got context for this path within
  ``COGNEE_FILE_CONTEXT_TTL`` seconds (default 30 min): the model has it;
* server known down (connection marker in a definitive failure state) or the
  shared circuit breaker is open;
* no lane — the file is outside every indexed repo AND the graph lane is off.

Lanes (``COGNEE_FILE_CONTEXT_SCOPES``, default ``code``):

* ``code`` — deterministic ``query_facts`` filtered to the file, against the
  indexed repo's own dataset (``/code-index`` must have run for the repo);
* ``graph`` — a HYBRID_COMPLETION recall over the session dataset with the
  path as the query: notes, decisions and prior-session facts about the file.
  Off by default: it is an LLM-free vector+graph search, but it costs a graph
  round trip on every first read of a file.

Output: ``{"hookSpecificOutput": {"hookEventName": "PreToolUse",
"additionalContext": ...}}`` — the documented PreToolUse shape.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (  # noqa: E402
    DEFINITIVE_FAILURE_STATES,
    _float_env,
    _session_key_path_safe,
    elapsed_ms,
    get_session_key,
    hook_log,
    is_observer_child,
    load_resolved,
    quiet_hook_output,
    read_connection_state,
    recall_via_http,
    resolve_session_key_from_payload,
    set_session_key,
)

ENV_ENABLED = "COGNEE_FILE_CONTEXT"
ENV_SCOPES = "COGNEE_FILE_CONTEXT_SCOPES"
ENV_BUDGET = "COGNEE_FILE_CONTEXT_BUDGET"
ENV_TTL = "COGNEE_FILE_CONTEXT_TTL"
ENV_MAX_SYMBOLS = "COGNEE_FILE_CONTEXT_MAX_SYMBOLS"

DEFAULT_BUDGET = 3.0
DEFAULT_TTL = 1800.0
DEFAULT_MAX_SYMBOLS = 40
GRAPH_TOP_K = 3
GRAPH_SNIPPET = 400
# Below this much of the budget a request cannot return anything useful.
MIN_LANE_TIMEOUT = 0.3
# Enough for a large file's facts: one line per symbol plus a call target list.
CODE_LIMIT = 200

_SEEN_DIR = Path.home() / ".cognee-plugin" / "claude-code" / "file-context"
# A seen-file grows one entry per distinct path; cap it so a long session that
# reads thousands of files does not turn the marker into a liability.
_SEEN_MAX_ENTRIES = 2000


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "true").strip().lower() not in ("0", "false", "no", "off")


def scopes() -> list[str]:
    """The lanes to run: a subset of {"code", "graph"}, ``["code"]`` by default."""
    raw = os.environ.get(ENV_SCOPES, "code")
    wanted = [p.strip().lower() for p in raw.split(",") if p.strip()]
    out = [s for s in ("code", "graph") if s in wanted]
    return out or ["code"]


def file_path_from(payload: dict) -> str:
    """The absolute path ``Read`` is about to open, or "" when there is none."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    raw = str(tool_input.get("file_path") or "").strip()
    if not raw:
        return ""
    cwd = str(payload.get("cwd") or "")
    path = raw if os.path.isabs(raw) else os.path.join(cwd, raw) if cwd else raw
    return os.path.normpath(path)


def sensitive(path: str) -> bool:
    try:
        from _capture_policy import _sensitive_path

        return bool(_sensitive_path(path))
    except Exception:
        return False


def server_known_bad() -> str:
    """ "unreachable"/"breaker_open"/... when a call would only add latency, else ""."""
    try:
        state = str(read_connection_state().get("state") or "")
        if state in DEFINITIVE_FAILURE_STATES:
            return state
    except Exception:
        pass
    try:
        from _cognee_client import breaker_open

        is_open, _retry = breaker_open()
        if is_open:
            return "breaker_open"
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Seen marker: one context per (session, path) per TTL
# ---------------------------------------------------------------------------


def _seen_path(session_key: str) -> Path | None:
    if not _session_key_path_safe(session_key):
        return None
    return _SEEN_DIR / f"{session_key}.json"


def _read_seen(session_key: str) -> dict:
    path = _seen_path(session_key)
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def recently_seen(session_key: str, file_path: str, ttl: float, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        stamp = float(_read_seen(session_key).get(file_path) or 0)
    except (TypeError, ValueError):
        return False
    return bool(stamp) and (now - stamp) < ttl


def mark_seen(session_key: str, file_path: str, now: float | None = None) -> None:
    path = _seen_path(session_key)
    if path is None:
        return
    now = time.time() if now is None else now
    try:
        seen = _read_seen(session_key)
        seen[file_path] = now
        if len(seen) > _SEEN_MAX_ENTRIES:
            oldest = sorted(seen, key=lambda k: float(seen.get(k) or 0))
            for key in oldest[: len(seen) - _SEEN_MAX_ENTRIES]:
                seen.pop(key, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(seen), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        hook_log("file_context_error", {"stage": "mark_seen", "error": str(exc)[:200]})


# ---------------------------------------------------------------------------
# Code lane: facts about this file from the indexed repo's code graph
# ---------------------------------------------------------------------------


def code_lane_for(file_path: str) -> dict:
    """``{"dataset", "repo_root", "rel"}`` when the file sits in an indexed repo, else {}."""
    try:
        from _code_graph import CODE_EXTENSIONS, find_indexed_repo

        # CODE_EXTENSIONS holds bare extensions ("py", not ".py").
        if Path(file_path).suffix.lower().lstrip(".") not in CODE_EXTENSIONS:
            return {}
        state = find_indexed_repo(os.path.dirname(file_path))
        if not state or not state.get("dataset") or not state.get("repo_root"):
            return {}
        root = str(state["repo_root"])
        rel = os.path.relpath(os.path.realpath(file_path), root).replace(os.sep, "/")
        if rel.startswith(".."):
            return {}
        return {
            "dataset": str(state["dataset"]),
            "repo_root": root,
            "rel": rel,
            "last_index_at": state.get("last_index_at"),
        }
    except Exception as exc:
        hook_log("file_context_error", {"stage": "code_lane", "error": str(exc)[:200]})
        return {}


def edited_since_index(file_path: str, last_index_at: object) -> bool:
    """Whether the file changed after the code graph's snapshot of its repo.

    ``last_index_at`` is stamped before the server reads the tree (first index
    and every Stop-hook re-index alike), so an mtime past it means the graph
    predates the file on disk: symbols likely still hold, line numbers may not.
    Unknown on either side (no stamp, unreadable file) is "not edited" — the
    note is a hint, and a missing hint only restores the pre-note behaviour.
    A re-index that was submitted but is still processing server-side is not
    caught; the stamp cannot tell submitted from done.
    """
    try:
        stamp = float(last_index_at or 0)
        return stamp > 0 and os.stat(file_path).st_mtime > stamp
    except (TypeError, ValueError, OSError):
        return False


def facts_from(results: list) -> list[dict]:
    """Flatten the code-scope recall entries into their fact dicts."""
    facts: list[dict] = []
    for entry in results or []:
        if not isinstance(entry, dict):
            continue
        body = entry.get("raw")
        if not isinstance(body, dict):
            text = entry.get("text") or entry.get("content") or ""
            try:
                body = (
                    json.loads(text)
                    if isinstance(text, str) and text.strip().startswith("{")
                    else {}
                )
            except ValueError:
                body = {}
        for fact in body.get("facts") or []:
            if isinstance(fact, dict):
                facts.append(fact)
    return facts


def _short(name: str, rel: str) -> str:
    """``pkg/mod.Class.method`` -> ``Class.method`` for symbols of this file."""
    stem = rel.rsplit(".", 1)[0]
    if name.startswith(stem + "."):
        return name[len(stem) + 1 :]
    return name


def _module_of(name: str) -> str:
    """``pkg/mod.Class.method`` -> ``pkg/mod`` (the path-like part before the first dot)."""
    return name.split(".", 1)[0]


def _plural(kind: str) -> str:
    if kind.endswith(("s", "x", "ch", "sh")):
        return kind + "es"
    return kind + "s"


def format_code_facts(facts: list[dict], rel: str, max_symbols: int = DEFAULT_MAX_SYMBOLS) -> str:
    """A compact map of the file: symbols by kind with lines, external calls, imports.

    Only what a reader cannot get faster from the file itself: line numbers to
    jump to, and the cross-file edges (calls out of the file, dependencies)
    that a single file does not show.
    """
    symbols: list[dict] = []
    deps: list[str] = []
    calls_out: dict[str, set] = {}
    stem = rel.rsplit(".", 1)[0]
    for fact in facts:
        kind = str(fact.get("kind") or "")
        name = str(fact.get("name") or "")
        if kind in ("symbol", "file_ref"):
            if kind == "symbol":
                symbols.append(fact)
            # file_ref relations are the module-level calls (imports used at import time).
            for relation in fact.get("relations") or []:
                if not isinstance(relation, dict) or relation.get("type") != "calls":
                    continue
                target = str(relation.get("target") or "")
                if target and not target.startswith(stem + "."):
                    calls_out.setdefault(_module_of(target), set()).add(target.rsplit(".", 1)[-1])
        elif kind == "dependency":
            # "pkg/mod -> requests"
            dep = name.split("->", 1)[1].strip() if "->" in name else name
            if dep and dep not in deps:
                deps.append(dep)
    if not symbols and not deps:
        return ""

    symbols.sort(key=lambda f: (int(f.get("line") or 0), str(f.get("name") or "")))
    shown = symbols[:max_symbols]
    by_kind: dict[str, list[str]] = {}
    for fact in shown:
        props = fact.get("properties") if isinstance(fact.get("properties"), dict) else {}
        skind = str(props.get("symbol_kind") or "symbol")
        line = fact.get("line")
        label = _short(str(fact.get("name") or ""), rel)
        extra = ""
        if props.get("exported") is False:
            extra = " (private)"
        entry = f"{label}:{line}{extra}" if line else f"{label}{extra}"
        by_kind.setdefault(skind, []).append(entry)

    lines = [f"Symbols in {rel} (name:line):"]
    order = [
        "class",
        "function",
        "method",
        "interface",
        "struct",
        "enum",
        "type",
        "variable",
        "constant",
    ]
    for skind in sorted(by_kind, key=lambda k: (order.index(k) if k in order else len(order), k)):
        lines.append(f"  {_plural(skind)}: " + ", ".join(by_kind[skind]))
    if len(symbols) > len(shown):
        lines.append(f"  … {len(symbols) - len(shown)} more symbols not shown")
    if calls_out:
        parts = []
        for module in sorted(calls_out)[:12]:
            names = sorted(calls_out[module])
            head = ", ".join(names[:5]) + (f", +{len(names) - 5}" if len(names) > 5 else "")
            parts.append(f"{module} ({head})")
        lines.append("Calls out to: " + "; ".join(parts))
    if deps:
        lines.append(
            "Imports: " + ", ".join(deps[:20]) + (f", +{len(deps) - 20}" if len(deps) > 20 else "")
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Graph lane: what memory says about this file
# ---------------------------------------------------------------------------


def format_graph_hits(results: list) -> str:
    snippets: list[str] = []
    for entry in results or []:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("content") or entry.get("text") or "").strip()
        if not text:
            continue
        snippets.append(text[:GRAPH_SNIPPET].replace("\n", " "))
        if len(snippets) >= GRAPH_TOP_K:
            break
    if not snippets:
        return ""
    return "Memory about this file:\n" + "\n".join(f"  - {s}" for s in snippets)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_context(file_path: str, lane: dict, resolved: dict, deadline: float) -> tuple[str, dict]:
    """Run the enabled lanes inside the deadline; returns (context, stats).

    ``lane`` is ``code_lane_for(file_path)`` computed once by the caller ({}
    when the file is outside every indexed repo). ``stats["answered"]`` is True
    when at least one lane got a server response — empty or not — as opposed
    to every lane erroring, timing out, or being skipped for lack of budget.
    """
    stats: dict = {"code": 0, "graph": 0, "lanes": [], "answered": False}
    sections: list[str] = []
    lanes = scopes()
    session_id = str(resolved.get("session_id") or "")

    if "code" in lanes and lane:
        remaining = deadline - time.monotonic()
        if remaining >= MIN_LANE_TIMEOUT:
            stats["lanes"].append("code")
            t0 = time.monotonic()
            try:
                results = recall_via_http(
                    lane["rel"],
                    session_id=session_id,
                    top_k=1,
                    scope=["code"],
                    dataset=lane["dataset"],
                    code_query={
                        "operation": "query_facts",
                        "file": lane["rel"],
                        "limit": CODE_LIMIT,
                    },
                    timeout=remaining,
                )
                stats["answered"] = True
                facts = facts_from(results)
                stats["code"] = len(facts)
                block = format_code_facts(
                    facts, lane["rel"], _int_env(ENV_MAX_SYMBOLS, DEFAULT_MAX_SYMBOLS)
                )
                if block:
                    if edited_since_index(file_path, lane.get("last_index_at")):
                        stats["stale"] = True
                        block += (
                            "\n(This file changed after the code graph was last indexed: "
                            "line numbers may have shifted — trust the file.)"
                        )
                    sections.append(block)
            except Exception as exc:
                hook_log(
                    "file_context_error",
                    {"stage": "code", "error": str(exc)[:200], "elapsed_ms": elapsed_ms(t0)},
                )
            stats["code_ms"] = elapsed_ms(t0)

    if "graph" in lanes and str(resolved.get("dataset") or ""):
        remaining = deadline - time.monotonic()
        if remaining >= MIN_LANE_TIMEOUT:
            stats["lanes"].append("graph")
            t0 = time.monotonic()
            try:
                query = lane["rel"] if lane else os.path.basename(file_path)
                results = recall_via_http(
                    query,
                    session_id=session_id,
                    top_k=GRAPH_TOP_K,
                    scope=["graph"],
                    search_type="HYBRID_COMPLETION",
                    dataset=str(resolved.get("dataset") or ""),
                    dataset_ids=list(resolved.get("dataset_ids") or []),
                    timeout=remaining,
                )
                stats["answered"] = True
                block = format_graph_hits(results)
                stats["graph"] = block.count("\n  - ") if block else 0
                if block:
                    sections.append(block)
            except Exception as exc:
                hook_log(
                    "file_context_error",
                    {"stage": "graph", "error": str(exc)[:200], "elapsed_ms": elapsed_ms(t0)},
                )
            stats["graph_ms"] = elapsed_ms(t0)

    if not sections:
        return "", stats
    body = "\n\n".join(sections)
    footer = (
        "(Cognee file context — from the code graph; for callers or impact use "
        '`cognee-search.sh "<symbol>" --code`.)'
    )
    return f"## Cognee: about {os.path.basename(file_path)}\n{body}\n{footer}", stats


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def emit(context: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": context,
                }
            }
        )
    )


def run(payload: dict) -> str:
    """The context to inject for this Read, or "" (every gate logs its reason)."""
    started = time.monotonic()
    file_path = file_path_from(payload)
    if not enabled():
        hook_log("file_context_skipped", {"reason": "disabled"})
        return ""
    if not file_path:
        hook_log("file_context_skipped", {"reason": "no_file_path"})
        return ""
    if sensitive(file_path):
        hook_log("file_context_skipped", {"reason": "sensitive_path"})
        return ""

    session_key_candidate, _source = resolve_session_key_from_payload(payload)
    if session_key_candidate:
        set_session_key(session_key_candidate)
    session_key = get_session_key()
    if not session_key:
        hook_log("file_context_skipped", {"reason": "no_session_key"})
        return ""

    ttl = _float_env(ENV_TTL, DEFAULT_TTL)
    if recently_seen(session_key, file_path, ttl):
        hook_log("file_context_skipped", {"reason": "seen"})
        return ""

    bad = server_known_bad()
    if bad:
        hook_log("file_context_skipped", {"reason": bad})
        return ""

    lanes = scopes()
    # Resolved once: the graph lane reuses it for its query, and each call
    # re-reads every repo index state from disk.
    lane = code_lane_for(file_path)
    if not ("code" in lanes and lane) and "graph" not in lanes:
        hook_log("file_context_skipped", {"reason": "no_lane"})
        return ""

    resolved = load_resolved(session_key, identity=False)
    deadline = started + _float_env(ENV_BUDGET, DEFAULT_BUDGET)
    context, stats = build_context(file_path, lane, resolved, deadline)
    # Mark only when the server answered: an empty answer is still an answer
    # (no point asking again within the TTL), but a timeout or error is not —
    # a cold first call must not cost the file its context for the whole TTL.
    if stats["answered"]:
        mark_seen(session_key, file_path)
    if not context:
        hook_log(
            "file_context_skipped", {"reason": "empty", **stats, "elapsed_ms": elapsed_ms(started)}
        )
        return ""
    hook_log(
        "file_context_injected", {**stats, "chars": len(context), "elapsed_ms": elapsed_ms(started)}
    )
    return context


def main() -> None:
    if is_observer_child():
        return
    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        hook_log("invalid_payload_json")
        return
    if not isinstance(payload, dict):
        return
    if str(payload.get("tool_name") or "Read") != "Read":
        return
    context = ""
    try:
        # Everything but the final JSON line goes to the plugin log: a stray
        # print or warning on stdout would corrupt the hook output.
        with quiet_hook_output("file-context"):
            context = run(payload)
    except Exception as exc:
        hook_log("file_context_error", {"stage": "run", "error": str(exc)[:200]})
    if context:
        emit(context)


if __name__ == "__main__":
    main()
