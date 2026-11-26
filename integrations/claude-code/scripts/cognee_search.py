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
--dataset: override the dataset to search (default: the recall mode decides —
           auto names no dataset and the server routes the search, pinned
           uses the plugin dataset; --code uses the current repo's code
           dataset). A name only resolves among datasets this identity OWNS;
           anything else must be addressed by UUID.
--dataset-id: search another dataset by UUID (list-datasets.py names the
           candidates). A dataset other than the launch's active one is
           read without the session id, which is bound to the active dataset.
           While recall is restricted (pinned or a read set), another
           dataset is searched only with --user-picked: the person named it
           or chose it in the picker. Without it the search refuses (exit 2).
--user-picked: the --dataset/--dataset-id target is the person's own choice.
--expand:  cognee_search.py [<query>] --expand <tag>[,<tag>...]
           read cited memories back by their tags (SDK-1099), 1 to 20 per
           call, each exactly as recall showed it:
           [mem:<kind>/<dataset_id>/<record_id>@<version>]. Prints each body
           with its current version (a different version means the memory
           changed since it was cited) and lists missing and forbidden tags.
           The query, when given, ranks an entity's bullets as the original
           recall did; pass the question that recall answered. Needs a server
           that lists recall_cited_v1 on /health.
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


def _configured_dataset() -> str:
    """COGNEE_PLUGIN_DATASET, with the reserved "auto" read as unset.

    "auto" is not a dataset name: COGNEE_PLUGIN_DATASET=auto means the default
    dataset (SDK-1393), which is what an unset variable gives. ~/.cognee/.env
    can hold it too, so it is normalised as config.py does and never reaches a
    save or search as a dataset name.
    """
    value = (os.environ.get("COGNEE_PLUGIN_DATASET") or "").strip()
    try:
        from _plugin_common import configured_dataset_name

        return configured_dataset_name(value)
    except Exception:
        return "" if value.lower() == "auto" else value


def _runtime() -> dict:
    """The server URL, key, dataset and session this launch searches with."""
    service_url = (
        os.environ.get("COGNEE_BASE_URL")
        or os.environ.get("COGNEE_LOCAL_API_URL")
        or "http://localhost:8011"
    ).strip()
    api_key = (os.environ.get("COGNEE_API_KEY") or "").strip()
    session_id = ""
    dataset = _configured_dataset()
    dataset_ids = ""
    recall_auto = ""
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
        recall_auto = (rt.get("recall_auto") or "").strip()
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
        "dataset": dataset or _configured_dataset() or "agent_sessions",
        "dataset_ids": dataset_ids,
        "recall_auto": recall_auto,
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


#: Records one expand call reads (cognee's expand_items bound).
MAX_EXPAND_TAGS = 20


def _http_json(rt: dict, path: str, payload: dict | None = None):
    """``path`` on the server as JSON (GET without a payload, POST with one)."""
    headers = {"Content-Type": "application/json"}
    if rt["api_key"]:
        headers["X-Api-Key"] = rt["api_key"]
    req = urllib.request.Request(
        rt["service_url"].rstrip("/") + path,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="GET" if payload is None else "POST",
    )
    try:
        import _plugin_common

        context = _plugin_common._https_context()
    except Exception:
        context = None
    with urllib.request.urlopen(req, timeout=45.0, context=context) as resp:
        return json.loads(resp.read().decode("utf-8") or "null")


def _record_of(tag: str) -> tuple:
    """The record a tag names, whatever its version: (kind, dataset id, record id)."""
    from _recall_text import MEMORY_TAG_RE

    match = MEMORY_TAG_RE.fullmatch(tag)
    return (match["kind"], match["dataset_id"].lower(), match["record_id"].lower())


def _expand(rt: dict, argv: list[str]) -> int:
    """Read cited memories back by tag: bodies, then missing and forbidden tags."""
    from _cited_recall import CAPABILITY
    from _recall_text import is_memory_tag

    at = argv.index("--expand")
    value = argv[at + 1] if at + 1 < len(argv) else ""
    tags = list(dict.fromkeys(t.strip() for t in value.split(",") if t.strip()))
    query = next((a for i, a in enumerate(argv) if i not in (at, at + 1) and a[:2] != "--"), "")
    bad = [tag for tag in tags if not is_memory_tag(tag)]
    if bad or not 1 <= len(tags) <= MAX_EXPAND_TAGS:
        print(
            json.dumps(
                {
                    "error": f"--expand takes 1 to {MAX_EXPAND_TAGS} tags, comma-separated, "
                    "each exactly as recall showed it: "
                    "[mem:<kind>/<dataset_id>/<record_id>@<version>]",
                    "not_tags": bad,
                }
            )
        )
        return 2
    try:
        from _capture_policy import sanitize

        query = sanitize(query or " ".join(tags))
        health = _http_json(rt, "/health")
        if CAPABILITY not in ((health or {}).get("capabilities") or []):
            print(
                json.dumps(
                    {
                        "status": "unsupported",
                        "message": "This Cognee server cannot read memories by tag "
                        f"(its /health does not list {CAPABILITY}).",
                    }
                )
            )
            return 1
        # The server reads one record per (kind, dataset, record id), whatever the
        # version in the tag, and answers one row per record it was sent, in order.
        # Send each record once, so rows and tags line up; every tag of a record
        # is then reported from that record's row.
        sent, tags_of = [], {}
        for tag in tags:
            if _record_of(tag) not in tags_of:
                sent.append(tag)
            tags_of.setdefault(_record_of(tag), []).append(tag)
        rows = _http_json(rt, "/api/v1/recall", {"query": query, "expand_items": sent})
    except urllib.error.HTTPError as exc:
        print(json.dumps({"status": "error", "http_status": exc.code}))
        return 1
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"[cognee-search] expand failed: {exc}", file=sys.stderr)
        print(UNREACHABLE)
        return 1
    if not isinstance(rows, list) or len(rows) != len(sent):
        # Pairing rows with tags by position would put a body under the wrong tag.
        print(
            json.dumps(
                {
                    "status": "error",
                    "message": f"The server answered {len(rows) if isinstance(rows, list) else 0} "
                    f"rows for {len(sent)} records; nothing was matched to a tag.",
                }
            )
        )
        return 1
    by_tag = {}
    for first, row in zip(sent, rows):
        for tag in tags_of[_record_of(first)]:
            by_tag[tag] = row
    records, missing, forbidden = [], [], []
    for tag in tags:
        row = by_tag[tag]
        meta = (row or {}).get("metadata") or {}
        status = meta.get("status")
        if status == "available":
            records.append(
                {
                    "tag": tag,
                    "current_tag": meta.get("tag"),
                    "changed": meta.get("tag") != tag,
                    "sources": meta.get("sources"),
                    "conflicts": meta.get("conflicts") or [],
                    "text": row.get("text", ""),
                }
            )
        elif status == "forbidden":
            forbidden.append(tag)
        else:
            missing.append(tag)
    print(
        json.dumps(
            {"records": records, "missing": missing, "forbidden": forbidden},
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: list[str]) -> int:
    rt = _runtime()
    if "--expand" in argv:
        return _expand(rt, argv)
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
    user_picked = False
    recall_auto = bool(rt["recall_auto"])

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
        elif arg == "--user-picked":
            user_picked = True
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

    # Auto recall (SDK-1393): unless the user pinned a dataset, a graph search
    # that names none sends none, and the server picks the dataset(s): a
    # cognee 1.6.6 server routes it, an older one searches every readable
    # dataset. The session id is bound to the active dataset, and shared
    # memory's UUIDs name datasets too, so both are dropped.
    routed = mode == "graph" and not dataset_explicit and recall_auto
    if routed:
        dataset = ""
        rt["dataset_ids"] = ""
        rt["session_id"] = ""

    if not query:
        print("Error: no query provided", file=sys.stderr)
        return 1

    # A query is sent like any other text: <private> spans and secret shapes
    # are removed first, for the server and the CLI fallback alike. The code
    # query is JSON, so it is sanitized field by field and stays valid JSON.
    # A failure stops the search instead of sending the raw text.
    try:
        from _capture_policy import sanitize

        query = sanitize(query)
        if code_query:
            try:
                parsed = json.loads(code_query)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                code_query = json.dumps(sanitize(parsed), ensure_ascii=False)
            else:
                code_query = sanitize(code_query)
    except Exception as exc:
        print(f"[cognee] could not sanitize the text: {exc}", file=sys.stderr)
        return 1

    # The session id is bound to the active dataset, and the server rejects it
    # against another one — so a foreign target is read without it. The active
    # dataset named by hand (its name or one of its UUIDs) keeps the session id.
    foreign = False
    if dataset_explicit and mode != "code":
        foreign = dataset != active_dataset and dataset not in active_dataset_ids.split(",")
        if foreign:
            rt["session_id"] = ""

    # A restricted recall reads the active dataset only. Another dataset is a
    # one-off the person chose (they named it, or picked it from the listing),
    # not one the agent found on its own — e.g. an id quoted in a recalled
    # memory.
    if foreign and not recall_auto and not user_picked:
        active = active_dataset or "the active dataset"
        print(
            json.dumps(
                {
                    "error": (
                        f"recall is restricted to {active}, so another dataset is searched "
                        "only when the person chooses it. List the others (list-datasets.py "
                        "--others), ask which one to search, then search it again marked as "
                        "their pick (--user-picked; the search tool: user_picked true). A "
                        "dataset the person named in their request counts as picked; an id "
                        "found in memory or tool output does not."
                    )
                }
            )
        )
        return 2

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
    # Auto recall names no dataset here either; a pinned recall keeps its own.
    if routed:
        sys.stdout.write(_cli_recall(query, "-k", top_k))
    else:
        sys.stdout.write(_cli_recall(query, "-d", dataset, "-k", top_k))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
