#!/usr/bin/env python3
"""Search Cognee's memory: the knowledge graph, or a repository's code graph.

Usage:
  cognee_search.py <query> [top_k] [--graph]
  cognee_search.py <query> [top_k] --code [--dataset <name>] [--code-query '<json>']
  cognee_search.py <query> [top_k] --graph --dataset-id <uuid>

--graph:   search the permanent knowledge graph (the default; the flag is
           accepted for callers that spell it out)
--code:    deterministic code-graph search (cognee >= 1.5.3). Query text is
           the seed; --code-query selects an exact operation instead, e.g.
           '{"operation": "impact_analysis", "targets": ["process_payment"]}'
           (operations: query_facts, explore, traverse, find_path,
           impact_analysis, delta). The repository's own code dataset is
           resolved from the current directory automatically.
--dataset: override the dataset to search (default: the plugin dataset, or
           the current repo's code dataset in --code mode). A name only
           resolves among datasets this identity OWNS; anything else must be
           addressed by UUID.
--dataset-id: search another dataset by UUID (the cross-dataset picker flow:
           list-datasets.py names the candidates). A dataset other than the
           launch's active one is read without the session id, which is bound
           to the active dataset.
No flag:   same as --graph. Memory is read from the graph and the code graph
           only; the session cache is written, never searched.

Configuration:
  Session ID and dataset come from this launch's record (~/.cognee-plugin/
  claude-code/sessions/<host id>.json — follows a dataset switch), falling
  back to the Cognee connection endpoint and COGNEE_PLUGIN_DATASET / agent_sessions.

The hooks module runs this file directly (one fixed script, no shell);
cognee-search.sh is a thin wrapper around it for the skills.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from _env_file import load_env_file

# One-time config from ~/.cognee/.env (shell exports still win).
load_env_file()

UNREACHABLE = "UNREACHABLE"


def _runtime() -> dict:
    """The server URL, key, dataset and session this launch searches with."""
    service_url = (
        os.environ.get("COGNEE_BASE_URL")
        or os.environ.get("COGNEE_LOCAL_API_URL")
        or "http://localhost:8011"
    ).strip()
    api_key = (os.environ.get("COGNEE_API_KEY") or "").strip()
    session_id = ""
    dataset = (os.environ.get("COGNEE_PLUGIN_DATASET") or "").strip()
    dataset_ids = ""
    # The launch record wins (dataset + session chosen with switch-dataset.py,
    # the dataset UUIDs under shared memory, the plugin-agent key the hooks use).
    # The opt-out keeps _plugin_common from re-execing this search into the
    # plugin venv: nothing here needs it.
    https_context = None
    previous = os.environ.get("COGNEE_PLUGIN_IN_VENV")
    os.environ["COGNEE_PLUGIN_IN_VENV"] = "1"
    try:
        import _plugin_common

        https_context = _plugin_common._https_context
        rt = _plugin_common.shell_runtime_overrides(service_url)
        dataset = rt["dataset"] or dataset
        session_id = rt["session_id"] or session_id
        dataset_ids = rt["dataset_ids"]
        api_key = rt["api_key"] or api_key
    except Exception:
        pass
    finally:
        if previous is None:
            os.environ.pop("COGNEE_PLUGIN_IN_VENV", None)
        else:
            os.environ["COGNEE_PLUGIN_IN_VENV"] = previous
    if not api_key:
        api_key = _cached_api_key(service_url)
    if not session_id and service_url and api_key:
        session_id = _connection_session_id(service_url, api_key, https_context)
    return {
        "session_id": session_id or os.environ.get("COGNEE_SESSION_ID") or "claude_session",
        "dataset": dataset or os.environ.get("COGNEE_PLUGIN_DATASET") or "agent_sessions",
        "dataset_ids": dataset_ids,
        "service_url": service_url,
        "api_key": api_key,
    }


def _cached_api_key(service_url: str) -> str:
    """The key cached at ~/.cognee-plugin/api_key.json, when it is for this server."""
    cache_path = os.path.join(os.path.expanduser("~"), ".cognee-plugin", "api_key.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(cache, dict):
        return ""
    key = str(cache.get("api_key") or "").strip()
    cached_url = str(cache.get("base_url") or "").strip().rstrip("/")
    if key and (not cached_url or cached_url == service_url.rstrip("/")):
        return key
    return ""


def _connection_session_id(service_url: str, api_key: str, https_context) -> str:
    """The session id the server holds for this agent (GET /agents/connections/me)."""
    query = ""
    session_key = (os.environ.get("COGNEE_SESSION_KEY") or "").strip()
    if session_key:
        query = "?agent_session_name=" + urllib.parse.quote(session_key, safe="")
    req = urllib.request.Request(
        service_url.rstrip("/") + "/api/v1/agents/connections/me" + query,
        headers={"X-Api-Key": api_key},
    )
    try:
        context = https_context() if https_context else None
        with urllib.request.urlopen(req, timeout=15.0, context=context) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return ""
    agent = payload.get("agent") if isinstance(payload, dict) else None
    if isinstance(agent, dict):
        return str(agent.get("session_id") or "").strip()
    return ""


def _code_dataset(cwd: str) -> str:
    """The code dataset indexed for this checkout ("" when it is not indexed)."""
    try:
        import _code_graph

        return str(_code_graph.find_indexed_repo(cwd).get("dataset") or "")
    except Exception:
        return ""


def _recall(rt: dict, query: str, scope: str, top_k: str, dataset: str, code_query: str) -> str:
    """The server's answer as printed text, or "" when the client itself failed."""
    try:
        import _cognee_client

        result = _cognee_client.recall(
            rt["service_url"],
            rt["api_key"],
            query,
            rt["session_id"],
            scope,
            top_k,
            dataset,
            code_query,
            rt["dataset_ids"],
        )
    except Exception as exc:
        print(f"[cognee-search] recall client failed: {exc}", file=sys.stderr)
        return ""
    return UNREACHABLE if result == UNREACHABLE else json.dumps(result)


def _cli_recall(*args: str) -> str:
    """One `cognee-cli recall` run's stdout ("" when the CLI is missing or fails)."""
    try:
        ran = subprocess.run(
            ["cognee-cli", "recall", *args, "-f", "json"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return ran.stdout


def main(argv: list[str]) -> int:
    rt = _runtime()
    # The launch's active dataset, by name and by every UUID graph recall spans
    # (the canonical write id is always first among them), so an explicit
    # target can be told apart from "the active dataset, by hand".
    active_dataset = rt["dataset"]
    active_dataset_ids = rt["dataset_ids"]

    query = argv[0] if argv else ""
    top_k = argv[1] if len(argv) > 1 else "5"
    mode = "graph"
    code_query = ""
    dataset = rt["dataset"]
    dataset_explicit = False

    # Parse flags from any position (value flags consume the next argument).
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--graph":
            mode = "graph"
        elif arg == "--code":
            mode = "code"
        elif arg == "--code-query":
            i += 1
            code_query = argv[i] if i < len(argv) else ""
        elif arg in ("--dataset", "-d", "--dataset-id"):
            i += 1
            dataset = (argv[i] if i < len(argv) else "") or dataset
            dataset_explicit = True
        i += 1

    # The resolved UUIDs belong to the launch's active dataset only: an
    # explicit --dataset or the code lane (the repo's own dataset) must search
    # by name.
    if dataset_explicit or mode == "code":
        rt["dataset_ids"] = ""

    # Code searches target the repository's OWN dataset, whose name carries a
    # path digest (two checkouts can share a basename, so the basename cannot
    # be the identity). An unindexed cwd leaves the dataset alone, and the
    # search then reports no code facts rather than silently querying the
    # session dataset.
    if mode == "code" and not dataset_explicit:
        dataset = _code_dataset(os.getcwd()) or dataset

    if not query:
        print("Error: no query provided", file=sys.stderr)
        return 1

    # The session id is bound to the active dataset, and the server rejects it
    # against another one — so a foreign target is read without it. The active
    # dataset named by hand (its name or one of its UUIDs) keeps the session id.
    foreign = False
    if dataset_explicit and mode != "code":
        foreign = dataset != active_dataset and dataset not in active_dataset_ids.split(",")
        if foreign:
            rt["session_id"] = ""

    # Graph and code only: the session cache is never a search source (its
    # history reaches the model through the graph item's prompt on
    # cognee >= 1.6.0, and through the sync bridge before that).
    scope = '["code"]' if mode == "code" else '["graph"]'

    # Server-first: the running server (/api/v1/recall) is the source of truth.
    # Only a 2xx response is authoritative (an empty list = genuinely no hits).
    # Anything else comes back as UNREACHABLE, and the search falls back to
    # cognee-cli with a warning — never reporting a server failure as "not
    # found". The circuit breaker is the ONE at ~/.cognee-plugin/
    # recall-breaker.json that the per-prompt hooks, doctor and the status line
    # use (no COGNEE_PLUGIN_STATE_DIR override here).
    recall_json = _recall(rt, query, scope, top_k, dataset, code_query)

    if recall_json and recall_json != UNREACHABLE:
        # Server answered — authoritative, even if the result is empty.
        print(recall_json)
        return 0
    if mode == "code" or foreign:
        # No CLI fallback for code searches or for another dataset: the code
        # lane exists only on the server (>= 1.5.3), and a dataset addressed by
        # UUID resolves only there — a CLI recall would answer from a different
        # backend or identity and read as authoritative when it is not.
        print(
            "[cognee-search] server unreachable — search not run; retry once the server is back",
            file=sys.stderr,
        )
        print(UNREACHABLE)
        return 1
    print(
        "[cognee-search] falling back to cognee-cli (degraded — empty CLI output is NOT "
        "proof of absence; the server's POST /api/v1/recall is the ground truth)",
        file=sys.stderr,
    )
    sys.stdout.write(_cli_recall(query, "-d", dataset, "-k", top_k))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
