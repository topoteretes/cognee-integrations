#!/usr/bin/env python3
"""Ask the code graph how the code works: seeds, flows and the lines to read.

The helper behind the cognee-ask-code skill (entry point:
``cognee-ask-code.py``). It runs the code graph's navigation operations
(SDK-1119: ``capabilities``, ``search``, ``neighbors``, ``subgraph``,
``flows``, ``hubs``, ``ask``) as a ``code_query`` on ``POST /api/v1/recall``
with ``scope: ["code"]``, and prints a compact, citation-first answer: every
node as ``path:line``, so the agent knows which lines to read before it says
how the code behaves.

Usage::

    cognee-ask-code.py ask "<question>" [--max-seeds N] [--include-tests]
    cognee-ask-code.py search "<text>" [--kinds symbol,route,flow] [--limit N]
                              [--include-tests]
    cognee-ask-code.py neighbors <name-or-id> [--direction both|forward|reverse]
                                 [--relation calls] [--limit N]
    cognee-ask-code.py subgraph <seed> [<seed> ...] [--depth 1-3] [--max-nodes N]
    cognee-ask-code.py flows [list [--limit N] | show <flow-name-or-id> | through <symbol>]
    cognee-ask-code.py hubs [--limit N] [--include-tests]
    cognee-ask-code.py capabilities
    every command: [--dataset <name-or-uuid>] [--json]

Server, key and dataset resolve as ``cognee-search.sh --code`` resolves them:
``COGNEE_BASE_URL`` (then ``COGNEE_LOCAL_API_URL``, then the local default),
the plugin-agent key or ``COGNEE_API_KEY``, and the code dataset of the
repository around the current directory, which here also finds a repository
indexed by git URL through the checkout's remotes; ``--dataset`` overrides it.
``ask`` probes ``capabilities`` first; the other commands call their operation
directly. ``--json`` prints the operation's payload as the server returned it.
Each request times out after 45 s (``COGNEE_ASK_CODE_TIMEOUT`` overrides); the
best-effort candidate search after an empty name lookup waits at most 10 s.

Exit statuses (the skill tells the agent what to do for each):

  0  printed a result.
  2  usage error: bad arguments, or an argument the server rejected
     (code ``invalid_argument``).
  3  FALLBACK: the server lacks these operations (code ``unknown_operation``,
     a 422 naming an unsupported or unknown operation, or capabilities
     without ``ask``). Use the cognee-code operations and grep instead.
  4  NO CODE GRAPH: the dataset has no code graph (an empty capabilities or
     ask result, HTTP 404, zero nodes, or code ``no_code_graph``).
  5  UNAVAILABLE: unreachable, timed out, 401/403, 5xx or a malformed reply.
  6  NOT FOUND: code ``not_found``/``ambiguous``, or a navigation command
     found nothing (recall turns a seed the graph cannot resolve into an
     empty result).
  7  FEATURE UNAVAILABLE: code ``feature_unavailable``, e.g. flows on a
     snapshot indexed without Enola's flows.json.

Recall reports a seed miss, a missing graph and a missing feature all as an
empty result, so an empty navigation result is followed by one
``capabilities`` probe that tells 4, 6 and 7 apart, and a name that matched
nothing by one ``search`` whose best hits stand in for the candidates recall
dropped. Output beyond about 120 lines is cut and reported like the server's
own truncation. Every status but 0 prints
one short paragraph to stdout starting with "Cognee code graph:", so the
agent sees it; diagnostics go to stderr. Stdlib only and Python 3.9
compatible, like the other plugin scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import traceback
from dataclasses import dataclass

from _code_graph import (
    _run_git,
    default_code_dataset,
    find_indexed_repo,
    git_repo_root,
    is_remote_repo,
    load_repo_states,
)
from _cognee_client import recall
from _plugin_common import (
    _DEFAULT_LOCAL_SERVICE_URL,
    active_agent_key,
    load_cached_api_key,
    positive_float_env,
)
from _recall_http import UNREACHABLE

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FALLBACK = 3
EXIT_NO_CODE_GRAPH = 4
EXIT_UNAVAILABLE = 5
EXIT_NOT_FOUND = 6
EXIT_FEATURE_UNAVAILABLE = 7

DEFAULT_TIMEOUT_SECONDS = 45.0
#: Printed output stays near this many lines; ``--json`` prints everything.
MAX_LINES = 120

_HELPER = "cognee-ask-code.py"
_SCOPE = '["code"]'
# Recall's request carries a top_k; the code retriever ignores it.
_TOP_K = 10
_DOC_CHARS = 110

# What a server without these operations answers (HTTP 422, no ``code`` field
# before SDK-1119): "Unsupported CODE operation 'ask'; expected one of ...".
_UNKNOWN_OPERATION_RE = re.compile(r"unsupported code operation|unknown operation", re.I)
# Cognee node ids (UUIDs) and Enola fact ids (32 hex chars) are sent as ids;
# anything else is a name, which the server matches exactly, then by suffix,
# then as a substring.
_ID_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})$", re.I
)
# The ask notes that report a cut, in the server's fixed wording.
_CUT_NOTES = (
    (re.compile(r"neighbourhood was cut at (\d+)", re.I), "neighbourhood cut at %s nodes"),
    (re.compile(r"citations were cut at (\d+)", re.I), "citations cut at %s"),
)
_SUBGRAPH_CUTS = {
    "max_seeds": "only the first {seeds} seeds were used",
    "max_depth": "depth capped at {depth}",
    "max_nodes": "stopped at {max_nodes} nodes",
    "max_edges": "stopped at {max_edges} edges",
}
_TRUNCATED = "truncated: yes ("
# Search hits shown in place of the candidates recall drops from a name miss.
_CANDIDATES = 5
# That search is best effort and follows two full requests (the lookup and the
# capabilities probe), so it gets a short timeout of its own.
_CANDIDATE_TIMEOUT_SECONDS = 10.0
# What a name lookup can resolve: search's default kinds without flows, which
# only ``flows show`` looks up.
_LOOKUP_KINDS = ["symbol", "route", "module", "storage", "service"]
# Repositories indexed by git URL that an exit-4 message names.
_URL_HINTS = 3
# A git URL as ``host`` and ``path``: scheme URLs (https, ssh, git) and the
# scp-like ``user@host:path``, so every spelling of one remote compares equal.
_SCHEME_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)(?::\d*)?/(.*)$", re.I)
_SCP_URL_RE = re.compile(r"^(?:[^@/]*@)?([^/:]+):(?!/)(.*)$")


class Stop(Exception):
    """Ends a command with an exit status and one message for the agent."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Target:
    """Where the requests go. ``explicit`` is True when --dataset named it."""

    service_url: str
    api_key: str
    dataset: str
    explicit: bool = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env(name):
    return str(os.environ.get(name, "") or "").strip()


def code_dataset(cwd):
    """The code dataset of the repository around ``cwd``.

    The index state recorded for the checkout names it, as for
    ``cognee-search.sh --code``. A repository indexed by git URL (the only
    way a cloud server can index one) has no local path to match, so a URL
    state that is one of the checkout's remotes names it next. Without
    either, the name the index command gives the enclosing git root (or
    ``cwd``) is used, so a repo indexed under another HOME still resolves;
    an unindexed one answers HTTP 404, which is exit 4.
    """
    state = find_indexed_repo(cwd)
    if state.get("dataset"):
        return str(state["dataset"])
    root = git_repo_root(cwd)
    state = url_indexed_repo(root)
    if state.get("dataset"):
        return str(state["dataset"])
    return default_code_dataset(root or cwd)


def repo_key(url):
    """``host/path`` of a git URL, lowercased and without ``.git``, or ''.

    The https, ssh and scp-like spellings of one remote give the same key, so
    a repository indexed as ``https://host/org/repo`` matches a checkout
    whose origin is ``git@host:org/repo.git``. Local paths give ''.
    """
    text = str(url or "").strip()
    match = _SCHEME_URL_RE.match(text) or _SCP_URL_RE.match(text)
    if not match:
        return ""
    path = match.group(2).strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")].rstrip("/")
    return ("%s/%s" % (match.group(1), path)).lower() if path else ""


def _url_states():
    """The recorded states of repositories indexed by git URL, newest first."""
    states = [
        state
        for state in load_repo_states()
        if state.get("dataset") and is_remote_repo(str(state.get("spec") or ""))
    ]

    def indexed_at(state):
        try:
            return float(state.get("last_index_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    return sorted(states, key=indexed_at, reverse=True)


def url_indexed_repo(root):
    """The state of a repository indexed by git URL that ``root`` has as a remote, or {}.

    Origin's URL wins over other remotes; among states for one remote, the
    most recent index wins.
    """
    states = _url_states()
    if not root or not states:
        return {}
    remotes = []
    for line in _run_git(["remote", "-v"], root).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes.append((parts[0] != "origin", repo_key(parts[1])))
    keys = []
    for _, key in sorted(remotes, key=lambda remote: remote[0]):
        if key and key not in keys:
            keys.append(key)
    matches = [state for state in states if repo_key(state.get("spec")) in keys]
    if not matches:
        return {}
    return min(matches, key=lambda state: keys.index(repo_key(state.get("spec"))))


def resolve_target(dataset="", cwd=""):
    """Server, key and dataset, resolved as ``cognee-search.sh --code`` does.

    ``COGNEE_BASE_URL`` comes first, as in the skill wrappers (the hooks'
    ``_local_api_url`` prefers ``COGNEE_LOCAL_API_URL``): the code graph lives
    where ``cognee-index-repo.sh`` put it. The key is the plugin agent's when
    that identity is in force, else ``COGNEE_API_KEY``, else the cached key.
    """
    service_url = (
        _env("COGNEE_BASE_URL") or _env("COGNEE_LOCAL_API_URL") or _DEFAULT_LOCAL_SERVICE_URL
    )
    api_key = (
        active_agent_key(service_url) or _env("COGNEE_API_KEY") or load_cached_api_key(service_url)
    )
    named = str(dataset or "").strip()
    return Target(
        service_url=service_url,
        api_key=api_key,
        dataset=named or code_dataset(cwd or os.getcwd()),
        explicit=bool(named),
    )


def request_timeout():
    return positive_float_env("COGNEE_ASK_CODE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Messages (one paragraph each, always starting with "Cognee code graph:")
# ---------------------------------------------------------------------------


def fallback_message(target, why):
    # The dataset is named because cognee-search.sh --code resolves only a
    # repository indexed by path, not one found through its remote.
    return (
        "this server does not have the ask-the-code operations (%s). Use the cognee-code "
        "skill's operations instead: cognee-search.sh --code --dataset %s with query_facts "
        "(a name substring), explore, traverse or impact_analysis, plus grep, and read the "
        "files before you answer." % (why, _quote(target.dataset))
    )


def no_code_graph_message(target, why):
    message = "dataset %s has no code graph (%s). " % (target.dataset, why)
    index = (
        "the repository first (cognee-code skill, cognee-index-repo.sh), or answer from grep "
        "and the files."
    )
    # A repository indexed by git URL that is not one of this checkout's
    # remotes (a fork, a mirror) is not found by code_dataset, and indexing it
    # again would rewrite that same dataset and end here again.
    indexed = [
        "%s (dataset %s)" % (state.get("spec"), state["dataset"])
        for state in _url_states()
        if state["dataset"] != target.dataset
    ][:_URL_HINTS]
    if not indexed:
        return message + "Index " + index
    return message + (
        "Repositories indexed by git URL from this machine: %s. If one of them is this "
        "repository, rerun with --dataset <its dataset> instead of indexing it again; "
        "otherwise index %s" % (", ".join(indexed), index)
    )


def unavailable_message(why):
    return (
        "unavailable (%s). Answer from grep and the files, and say the code graph was not "
        "used." % why
    )


def not_found_message(target, subject, detail="", ambiguous=False, candidates=""):
    message = "nothing in dataset %s matched %s" % (target.dataset, subject)
    if ambiguous:
        message += ", or more than one did"
    if detail:
        message += " (%s)" % detail
    if candidates:
        return message + (
            ". Search candidates: %s. Rerun with one of their full names or ids, or grep for "
            "the name." % candidates
        )
    return message + ('. Search for it (%s search "<words>") or grep for the name.' % _HELPER)


def feature_unavailable_message(target, detail=""):
    message = (
        "dataset %s has no flows: they come from Enola's flows.json, which this snapshot "
        "was indexed without (re-index with an Enola release that writes it)" % target.dataset
    )
    if detail:
        message += " (%s)" % detail
    return message + ". Skip flows; use search, neighbors or subgraph."


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def code_payload(results):
    """The operation payload in a recall result list, or None when it has none.

    Recall wraps a CODE payload as an entry with ``source == "code"``: the
    payload under ``raw`` and its JSON dump under ``text``. An empty list is
    recall's "no code facts".
    """
    entries = [item for item in results if isinstance(item, dict) and item.get("source") == "code"]
    for entry in entries:
        raw = entry.get("raw")
        if isinstance(raw, dict) and raw.get("operation"):
            return raw
        try:
            parsed = json.loads(entry.get("text") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed.get("operation"):
            return parsed
    if entries:
        raise Stop(
            EXIT_UNAVAILABLE,
            unavailable_message("the server's code result carried no operation payload"),
        )
    return None


def error_stop(target, operation, envelope):
    """The Stop for an error envelope from the recall transport.

    The envelope's ``code`` (servers with SDK-1119) decides; an older server
    is recognised by its message. Everything else, 401/403, 5xx, a malformed
    reply and an open circuit breaker included, is unavailable. HTTP 404 never
    gets here: the transport reports it as an empty result.
    """
    code = str(envelope.get("code") or "")
    param = str(envelope.get("param") or "")
    message = str(envelope.get("error") or "the request failed")
    if envelope.get("transient"):
        return Stop(
            EXIT_UNAVAILABLE,
            unavailable_message("no answer from %s: %s" % (target.service_url, message)),
        )
    old_server = envelope.get("status") == 422 and _UNKNOWN_OPERATION_RE.search(message)
    if code == "unknown_operation" or old_server:
        return Stop(
            EXIT_FALLBACK, fallback_message(target, "it rejected %s: %s" % (operation, message))
        )
    if code == "no_code_graph":
        return Stop(EXIT_NO_CODE_GRAPH, no_code_graph_message(target, message))
    if code in ("not_found", "ambiguous"):
        subject = "the %s" % (param or "seed")
        return Stop(EXIT_NOT_FOUND, not_found_message(target, subject, message))
    if code == "feature_unavailable":
        return Stop(EXIT_FEATURE_UNAVAILABLE, feature_unavailable_message(target, message))
    if code == "invalid_argument":
        return Stop(
            EXIT_USAGE,
            "the server rejected %s: %s. Fix it and retry." % (param or "an argument", message),
        )
    return Stop(EXIT_UNAVAILABLE, unavailable_message(message))


def run(target, code_query, text="", timeout=None):
    """Run one CODE operation: its payload, or None for an empty result.

    Goes through the plugin's shared recall client, so a server the hooks'
    circuit breaker gave up on is not hammered from here either. Raises Stop
    for anything that is not a result.
    """
    operation = code_query["operation"]
    result = recall(
        target.service_url,
        target.api_key,
        # Recall needs a non-empty query; operations with no text ignore it.
        text or operation,
        "",
        _SCOPE,
        _TOP_K,
        target.dataset,
        code_query=code_query,
        timeout=timeout or request_timeout(),
    )
    if result == UNREACHABLE:
        raise Stop(
            EXIT_UNAVAILABLE, unavailable_message("nothing answers at %s" % target.service_url)
        )
    if isinstance(result, dict):
        raise error_stop(target, operation, result)
    if not isinstance(result, list):
        raise Stop(EXIT_UNAVAILABLE, unavailable_message("a malformed reply from the server"))
    return code_payload(result)


def probe(target):
    """The capabilities payload of a dataset that has a code graph (else Stop 4)."""
    payload = run(target, {"operation": "capabilities"})
    if payload is None:
        raise Stop(
            EXIT_NO_CODE_GRAPH, no_code_graph_message(target, "capabilities returned nothing")
        )
    if _count(payload, "nodes") == 0:
        raise Stop(EXIT_NO_CODE_GRAPH, no_code_graph_message(target, "it has no nodes"))
    return payload


def explain_empty(target, code_query, subject):
    """The Stop for an empty navigation result: no graph (4), no flows (7), no match (6)."""
    operation = code_query["operation"]
    try:
        caps = run(target, {"operation": "capabilities"})
    except Stop as stop:
        # Unavailable and no code graph are definite answers. Anything else (an
        # older server without capabilities) cannot tell, and recall's empty
        # result is a seed miss by default.
        if stop.status in (EXIT_UNAVAILABLE, EXIT_NO_CODE_GRAPH):
            raise
        caps = {}
    if caps is None or _count(caps, "nodes") == 0:
        return Stop(EXIT_NO_CODE_GRAPH, no_code_graph_message(target, "the result was empty"))
    if operation == "flows" and _dict(caps.get("features")).get("flows") is False:
        return Stop(EXIT_FEATURE_UNAVAILABLE, feature_unavailable_message(target))
    # A name that matches several nodes is as empty through recall as one that
    # matches none, so a lookup by name cannot tell the two apart.
    lookup = operation in ("neighbors", "subgraph") or code_query.get("mode") in ("show", "through")
    found = search_candidates(target, code_query, caps) if lookup else ""
    return Stop(
        EXIT_NOT_FOUND, not_found_message(target, subject, ambiguous=lookup, candidates=found)
    )


def search_candidates(target, code_query, caps):
    """The best search hits for the names a lookup missed, as text, or ''.

    Recall drops the server's not-found and ambiguous errors along with the
    candidates they list, so one ``search`` for the names stands in for them.
    Ids are not searched, and a failed search leaves the plain message.
    """
    if "search" not in _list(_dict(caps).get("operations")):
        return ""
    names = [code_query.get("name"), code_query.get("symbol")] + _list(code_query.get("seeds"))
    text = " ".join(str(name) for name in names if name).strip()
    if not text:
        return ""
    query = {
        "operation": "search",
        "text": text,
        "kinds": ["flow"] if code_query.get("mode") == "show" else _LOOKUP_KINDS,
        "include_tests": True,
        "limit": _CANDIDATES,
    }
    try:
        payload = run(
            target, query, text, timeout=min(request_timeout(), _CANDIDATE_TIMEOUT_SECONDS)
        )
    except Stop:
        return ""
    return ", ".join(
        "%s (%s, id %s)"
        % (hit.get("name") or "?", location(hit) or hit.get("kind") or "?", hit["id"])
        for hit in map(_dict, _list(_dict(payload).get("hits"))[:_CANDIDATES])
        if hit.get("id")
    )


def navigate(target, code_query, text, subject):
    payload = run(target, code_query, text)
    if payload is None:
        raise explain_empty(target, code_query, subject)
    return payload


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _id_or(value, name_key):
    return "id" if _ID_RE.match(value) else name_key


def _quoted(text):
    return json.dumps(str(text), ensure_ascii=False)


def cmd_capabilities(args, target):
    return probe(target)


def cmd_ask(args, target):
    caps = probe(target)
    if "ask" not in _list(caps.get("operations")):
        raise Stop(
            EXIT_FALLBACK, fallback_message(target, "its capabilities list no ask operation")
        )
    query = {"operation": "ask", "question": args.question}
    if args.max_seeds:
        query["max_seeds"] = args.max_seeds
    if args.include_tests:
        query["include_tests"] = True
    payload = run(target, query, args.question)
    if payload is None:
        raise Stop(EXIT_NO_CODE_GRAPH, no_code_graph_message(target, "ask returned nothing"))
    return payload


def cmd_search(args, target):
    query = {"operation": "search", "text": args.text}
    if args.kinds:
        query["kinds"] = args.kinds
    if args.limit:
        query["limit"] = args.limit
    if args.include_tests:
        query["include_tests"] = True
    return navigate(target, query, args.text, _quoted(args.text))


def cmd_neighbors(args, target):
    query = {"operation": "neighbors", _id_or(args.anchor, "name"): args.anchor}
    if args.direction:
        query["direction"] = args.direction
    if args.relation:
        query["relation_types"] = args.relation
    if args.limit:
        query["limit"] = args.limit
    return navigate(target, query, args.anchor, _quoted(args.anchor))


def cmd_subgraph(args, target):
    query = {"operation": "subgraph"}
    names = [seed for seed in args.seeds if not _ID_RE.match(seed)]
    ids = [seed for seed in args.seeds if _ID_RE.match(seed)]
    if names:
        query["seeds"] = names
    if ids:
        query["seed_ids"] = ids
    if args.depth:
        query["depth"] = args.depth
    if args.max_nodes:
        query["max_nodes"] = args.max_nodes
    subject = "any of the seeds " + ", ".join(_quoted(seed) for seed in args.seeds)
    return navigate(target, query, " ".join(args.seeds), subject)


def cmd_flows(args, target):
    query = {"operation": "flows", "mode": args.mode}
    if args.mode == "list":
        if args.limit:
            query["limit"] = args.limit
        return navigate(target, query, "flows", "any flow")
    key = _id_or(args.target, "name" if args.mode == "show" else "symbol")
    query[key] = args.target
    # A symbol that resolves but no flow reaches is a result with no flows;
    # an empty result means the name itself matched nothing.
    what = "a flow" if args.mode == "show" else "a symbol"
    subject = "%s named %s" % (what, _quoted(args.target))
    return navigate(target, query, args.target, subject)


def cmd_hubs(args, target):
    query = {"operation": "hubs"}
    if args.limit:
        query["limit"] = args.limit
    if args.include_tests:
        query["include_tests"] = True
    return navigate(target, query, "hubs", "any hub")


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _int(value):
    """``value`` when it is a real int (bools are not), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count(payload, field):
    return _int(_dict(_dict(payload).get("counts")).get(field))


def _short(text, limit=_DOC_CHARS):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def location(node):
    """``path:line`` of a node or ref, ``path`` without a line, '' without a file."""
    node = _dict(node)
    path = str(node.get("file") or "")
    if not path:
        return ""
    line = _int(node.get("line"))
    return "%s:%d" % (path, line) if line is not None else path


def span(node):
    """``path:line-end`` for a node that spans several lines, else ``location``."""
    node = _dict(node)
    loc = location(node)
    line, end = _int(node.get("line")), _int(node.get("end_line"))
    if loc and line is not None and end is not None and end > line:
        return "%s-%d" % (loc, end)
    return loc


def label(node):
    """A node on one line: ``path:line  name [kind]``."""
    node = _dict(node)
    kind = node.get("symbol_kind") or node.get("kind") or ""
    text = "%s  %s" % (location(node) or "(no file)", node.get("name") or node.get("id") or "?")
    return text + (" [%s]" % kind if kind else "")


def doc_line(node, indent):
    doc = _dict(node).get("doc")
    return ["%s%s" % (indent, _short(doc))] if isinstance(doc, str) and doc.strip() else []


def truncated_line(reasons):
    return _TRUNCATED + "%s)" % "; ".join(reasons or ["see above"])


def _cut(blocks, limit):
    """``blocks`` cut to about ``limit`` lines: (lines, names of the cut blocks)."""
    blocks = [[list(block[0]), block[1], block[2] if len(block) > 2 else ""] for block in blocks]
    excess = sum(len(block[0]) for block in blocks) - limit
    order = sorted(
        (index for index, block in enumerate(blocks) if block[1] is not None),
        key=lambda index: blocks[index][1],
    )
    cut = []
    for index in order:
        if excess <= 0:
            break
        lines = blocks[index][0]
        keep = max(1, len(lines) - excess - 1)
        hidden = len(lines) - keep
        if hidden < 2:
            continue
        blocks[index][0] = lines[:keep] + [
            "  ... %d more lines not shown; --json prints the whole result" % hidden
        ]
        excess -= hidden - 1
        cut.append(index)
    names = [blocks[index][2] or "some lines" for index in sorted(cut)]
    return [line for block in blocks for line in block[0]], names


def fit(blocks, limit=MAX_LINES):
    """The lines of ``blocks`` cut to about ``limit``.

    ``blocks`` is a list of ``(lines, priority)``, or ``(lines, priority,
    name)`` for a block that can be cut. A block with priority None is always
    printed whole (headings, coverage, notes, the truncation line); the others
    are cut lowest priority first, each keeping its first line and saying how
    many lines it hid. Hidden lines are as unseen as what the server left
    out, so a cut is reported the same way: ask's Coverage line says
    ``truncated: yes`` and the output ends with a truncation line naming the
    cut blocks, merged into the result's own when it has one.
    """
    last = list(blocks[-1][0]) if blocks else []
    merge = len(last) == 1 and last[0].startswith(_TRUNCATED) and last[0].endswith(")")
    lines, names = _cut(blocks, limit)
    if names and not merge:
        lines, names = _cut(blocks, limit - 1)  # room for the line that reports the cut
    if not names:
        return lines
    reason = (
        "output capped at %d lines: %s partly hidden; rerun with --json or narrow the query"
        % (limit, ", ".join(names))
    )
    lines = [
        line[: -len("no")] + "yes"
        if line.startswith("Coverage: ") and line.endswith("; truncated: no")
        else line
        for line in lines
    ]
    if merge:
        return lines[:-1] + [lines[-1][:-1] + "; " + reason + ")"]
    return lines + [truncated_line([reason])]


def _quote(value):
    return shlex.quote(str(value))


# The code_query options each command has a flag for (see build_parser).
_FLAGS = {
    "ask": {"max_seeds": "--max-seeds", "include_tests": "--include-tests"},
    "search": {"kinds": "--kinds", "limit": "--limit", "include_tests": "--include-tests"},
    "neighbors": {"direction": "--direction", "relation_types": "--relation", "limit": "--limit"},
    "subgraph": {"depth": "--depth", "max_nodes": "--max-nodes"},
    "flows": {"limit": "--limit"},
    "hubs": {"limit": "--limit", "include_tests": "--include-tests"},
    "capabilities": {},
}


def helper_command(code_query, target):
    """The helper command that runs a server-suggested ``code_query``, or ''.

    '' when the query uses an operation or an option the helper has no flag
    for; the JSON is printed either way.
    """
    rest = dict(_dict(code_query))
    operation = str(rest.pop("operation", "") or "")
    flags = _FLAGS.get(operation)
    if flags is None:
        return ""

    def take(*keys):
        """Pop every spelling of one option; the first non-empty value wins."""
        found = None
        for key in keys:
            value = rest.pop(key, None)
            if found is None and value not in (None, "", []):
                found = value
        return found

    words = [operation]
    if operation in ("ask", "search"):
        text = take("question", "text", "query", "q")
        if not text:
            return ""
        words.append(_quote(text))
    elif operation == "neighbors":
        anchor = take("id", "node_id", "name")
        if not anchor:
            return ""
        words.append(_quote(anchor))
    elif operation == "subgraph":
        seeds = []
        for key in ("seed_ids", "node_ids", "ids", "seeds", "names"):
            seeds += [str(seed) for seed in _list(rest.pop(key, None))]
        if not seeds:
            return ""
        words += [_quote(seed) for seed in seeds]
    elif operation == "flows":
        mode = str(take("mode") or "")
        flow = take("flow", "name", "id")
        symbol = take("symbol", "through")
        if mode == "through" or (not mode and symbol):
            if not (symbol or flow):
                return ""
            words += ["through", _quote(symbol or flow)]
        elif mode == "show" or (not mode and flow):
            if not flow:
                return ""
            words += ["show", _quote(flow)]
        elif mode in ("", "list"):
            words.append("list")
        else:
            return ""
        if words[1] != "list" and "limit" in rest:
            return ""
    for key, value in rest.items():
        flag = flags.get(key)
        if flag is None:
            return ""
        if isinstance(value, bool):
            if value and flag == "--include-tests":
                words.append(flag)
            elif value:
                return ""
            continue
        if isinstance(value, list):
            value = ",".join(str(item) for item in value)
        words += [flag, _quote(value)]
    if target.explicit:
        words += ["--dataset", _quote(target.dataset)]
    return " ".join([_HELPER] + words)


# ---------------------------------------------------------------------------
# Renderers: each returns blocks for ``fit``
# ---------------------------------------------------------------------------


def _flow_head(flow, indent="  ", metrics=True):
    """``name  [mechanism, rank n]`` and, with ``metrics``, reach and unresolved calls."""
    flow = _dict(flow)
    tags = [str(flow.get("mechanism") or "flow")]
    if _int(flow.get("rank")) is not None:
        tags.append("rank %d" % flow["rank"])
    if flow.get("side"):
        tags.append("side")
    head = "%s%s  [%s]" % (indent, flow.get("name") or flow.get("id") or "?", ", ".join(tags))
    numbers = _dict(flow.get("metrics")) if metrics else {}
    facts = []
    if _int(numbers.get("reach")) is not None:
        facts.append("reach %d" % numbers["reach"])
    if _int(numbers.get("unresolved_calls")):
        facts.append("%d unresolved calls" % numbers["unresolved_calls"])
    return head + ("  " + ", ".join(facts) if facts else "")


def _flow_entry_and_stages(flow, indent="    ", docs=False):
    flow = _dict(flow)
    lines = []
    entry = _dict(flow.get("entry"))
    if entry:
        lines.append("%sentry  %s" % (indent, label(entry)))
        if docs:
            lines += doc_line(entry, indent + "       ")
    for stage in map(_dict, _list(flow.get("stages"))):
        depth = _int(stage.get("depth")) or 1
        pad = indent + "  " * depth
        order = _int(stage.get("order"))
        reach = _int(stage.get("reach"))
        text = "%s#%s  %s" % (pad, order if order is not None else "-", label(stage))
        lines.append(text + ("  (reach %d)" % reach if reach is not None else ""))
        if docs:
            lines += doc_line(stage, pad + "    ")
    return lines


def _seed_lines(seeds, mark_missing_docs):
    lines = []
    for hit in seeds:
        hit = _dict(hit)
        text = "  " + label(hit)
        matched = [str(m) for m in _list(hit.get("matched"))]
        if matched:
            text += "  matched " + ", ".join(matched)
        if mark_missing_docs and not hit.get("doc"):
            text += "  (no docstring)"
        lines.append(text)
        lines += doc_line(hit, "      ")
    return lines


def _ask_truncation(payload):
    reasons = []
    notes = [str(note) for note in _list(_dict(payload.get("coverage")).get("notes"))]
    for note in notes:
        for pattern, template in _CUT_NOTES:
            match = pattern.search(note)
            if match:
                reasons.append(template % match.group(1))
    for flow in map(_dict, _list(payload.get("flows"))):
        if _dict(flow.get("metrics")).get("truncated"):
            reasons.append("flow %s is larger than its walk" % (flow.get("name") or "?"))
    return reasons


def render_ask(payload, target):
    coverage = _dict(payload.get("coverage"))
    head = [
        "ask: %s (dataset %s)" % (payload.get("question") or "", target.dataset),
        "terms: %s" % (", ".join(str(t) for t in _list(payload.get("terms"))) or "none"),
    ]
    blocks = [(head, None)]

    seeds = _list(payload.get("seeds"))
    seed_lines = _seed_lines(seeds, bool(coverage.get("docs_present"))) or ["  none matched"]
    blocks.append((["Seeds"] + seed_lines, 3, "seeds"))

    flow_lines = []
    for flow in _list(payload.get("flows")):
        flow_lines.append(_flow_head(flow))
        flow_lines += _flow_entry_and_stages(flow)
    blocks.append((["Flows"] + (flow_lines or ["  none traced"]), 2, "flows"))

    hood = _dict(payload.get("neighborhood"))
    nodes = {str(_dict(n).get("id")): _dict(n) for n in _list(hood.get("nodes"))}
    seed_ids = {key for key, node in nodes.items() if node.get("role") == "seed"}
    seed_ids |= {str(_dict(s).get("id")) for s in seeds}
    # How each neighbour touches a seed, worded like the citations' "why". A
    # node next to several seeds takes the reason its citation gives (the
    # server's own link), else the first edge that fits its role.
    cited = {}
    for cite in map(_dict, _list(payload.get("citations"))):
        why = str(cite.get("why") or "")
        if why.startswith(("calls ", "called by ", "linked to ")):
            cited[str(cite.get("node_id"))] = why
    links = {}
    for edge in map(_dict, _list(hood.get("edges"))):
        source, dest = str(edge.get("source_id")), str(edge.get("target_id"))
        relation = str(edge.get("type") or "linked")
        if dest in seed_ids and source not in seed_ids:
            other, seed_name, side, calls = source, edge.get("target"), "caller", "calls %s"
        elif source in seed_ids and dest not in seed_ids:
            other, seed_name, side, calls = dest, edge.get("source"), "callee", "called by %s"
        else:
            continue
        if relation == "calls":
            links.setdefault(other, []).append((side, calls % seed_name))
        else:
            reason = "linked to %s by %s" % (seed_name, relation)
            links.setdefault(other, []).append(("neighbor", reason))
    hood_lines = []
    for key, node in nodes.items():
        if key in seed_ids:
            continue
        role = node.get("role") or "neighbor"
        reasons = links.get(key, [])
        why = cited.get(key) or next((w for r, w in reasons if r == role), "")
        why = why or next((w for _, w in reasons), "")
        line = "  %-8s  %s" % (role, label(node))
        hood_lines.append(line + ("  (%s)" % why if why else ""))
    hood_lines = hood_lines or ["  nothing adjacent"]
    blocks.append((["Neighbourhood (one hop)"] + hood_lines, 1, "neighbourhood"))

    cite_lines = []
    for cite in _list(payload.get("citations")):
        cite = _dict(cite)
        where = span(cite) or str(cite.get("cite") or "")
        line = "  %s  %s" % (where, cite.get("name") or cite.get("node_id") or "?")
        cite_lines.append(line + ("  (%s)" % cite["why"] if cite.get("why") else ""))
    citations = ["Citations (read these lines before answering)"]
    blocks.append((citations + (cite_lines or ["  none"]), 4, "citations"))

    seeds_count = _int(coverage.get("seeds"))
    with_docs = _int(coverage.get("seeds_with_docs"))
    if seeds_count is None:
        seeds_count = len(seeds)
    if with_docs is None:
        with_docs = sum(1 for seed in seeds if _dict(seed).get("doc"))
    docs = "docs on %d/%d seeds" % (with_docs, seeds_count)
    if with_docs < seeds_count:
        docs += " (%d missing)" % (seeds_count - with_docs)
    if coverage.get("docs_present") is False:
        docs += ", none in this snapshot"
    unresolved = _int(coverage.get("unresolved_calls"))
    truncated = bool(payload.get("truncated") or coverage.get("truncated"))
    blocks.append(
        (
            [
                "Coverage: %s; %s; unresolved calls: %s; truncated: %s"
                % (
                    docs,
                    "flows present" if coverage.get("flows_present") else "no flows",
                    unresolved if unresolved is not None else "unknown",
                    "yes" if truncated else "no",
                )
            ],
            None,
        )
    )

    notes = [str(note) for note in _list(coverage.get("notes"))]
    if notes:
        blocks.append((["Notes"] + ["  - %s" % note for note in notes], None))

    next_lines = []
    for step in _list(payload.get("next")):
        step = _dict(step)
        query = _dict(step.get("code_query"))
        if not query:
            continue
        line = "  - %s" % json.dumps(query, ensure_ascii=False)
        next_lines.append(line + ("  %s" % step["why"] if step.get("why") else ""))
        command = helper_command(query, target)
        if command:
            next_lines.append("    %s" % command)
    if next_lines:
        blocks.append((["Next"] + next_lines, None))

    if truncated:
        blocks.append(([truncated_line(_ask_truncation(payload))], None))
    return blocks


def render_search(payload, target):
    hits = _list(payload.get("hits"))
    total = _int(payload.get("total"))
    head = [
        "search: %s (dataset %s)" % (payload.get("query") or "", target.dataset),
        "terms: %s" % (", ".join(str(t) for t in _list(payload.get("terms"))) or "none"),
    ]
    lines = []
    for hit in hits:
        hit = _dict(hit)
        text = "  " + label(hit)
        if hit.get("kind") == "flow" and _int(hit.get("rank")) is not None:
            text += " rank %d" % hit["rank"]
        if hit.get("score") is not None:
            text += "  score %s" % hit["score"]
        matched = [str(m) for m in _list(hit.get("matched"))]
        if matched:
            text += "  matched " + ", ".join(matched)
        lines.append(text)
        lines += doc_line(hit, "      ")
    if not lines:
        lines = ["  nothing matched; try an exact identifier with neighbors, or grep"]
    blocks = [(head, None), (lines, 1, "hits")]
    if payload.get("has_more"):
        reason = "%d of %s hits shown; raise --limit (up to 50) or narrow --kinds" % (
            len(hits),
            total if total is not None else "more",
        )
        blocks.append(([truncated_line([reason])], None))
    return blocks


def render_neighbors(payload, target):
    anchor = _dict(payload.get("anchor"))
    head = ["neighbors of %s (dataset %s)" % (label(anchor), target.dataset)]
    lines = []
    for item in _list(payload.get("neighbors")):
        item = _dict(item)
        arrow = "<-" if item.get("direction") == "in" else "->"
        lines.append("  %s %s  %s" % (arrow, item.get("relation") or "?", label(item.get("node"))))
    blocks = [(head, None), (lines or ["  no neighbours"], 1, "neighbours")]
    flows = [
        "%s (%s)" % (_dict(f).get("name") or "?", _dict(f).get("as") or "member")
        for f in _list(payload.get("flows"))
    ]
    if flows:
        blocks.append((["in flows: " + ", ".join(flows)], None))
    total = _int(payload.get("total"))
    shown = len(_list(payload.get("neighbors")))
    if payload.get("truncated") or payload.get("has_more"):
        reason = "%d of %s neighbours shown; raise --limit (up to 200) or use --relation" % (
            shown,
            total if total is not None else "more",
        )
        blocks.append(([truncated_line([reason])], None))
    return blocks


def render_subgraph(payload, target):
    seeds = _list(payload.get("seeds"))
    nodes = _list(payload.get("nodes"))
    edges = _list(payload.get("edges"))
    stats = _dict(payload.get("stats"))
    head = "subgraph (dataset %s): seeds %d, nodes %d, edges %d" % (
        target.dataset,
        len(seeds),
        len(nodes),
        len(edges),
    )
    if _int(stats.get("depth_reached")) is not None:
        head += ", depth reached %d" % stats["depth_reached"]
    blocks = [([head], None)]
    blocks.append((["Seeds"] + ["  " + label(seed) for seed in seeds], 3, "seeds"))

    missing = []
    for item in _list(payload.get("seeds_missing")):
        item = _dict(item)
        line = "  %s: %s" % (item.get("seed") or "?", item.get("reason") or "not_found")
        candidates = [
            "%s [%s] %s" % (c.get("name") or c.get("id"), c.get("kind") or "?", c.get("file") or "")
            for c in map(_dict, _list(item.get("candidates")))
        ]
        if candidates:
            line += "; candidates: " + ", ".join(text.strip() for text in candidates)
        missing.append(line)
    if missing:
        blocks.append((["Missing seeds"] + missing, 4, "missing seeds"))

    seed_ids = {str(_dict(seed).get("id")) for seed in seeds}
    node_lines = []
    for node in nodes:
        node = _dict(node)
        if str(node.get("id")) in seed_ids:
            continue
        depth = _int(node.get("depth"))
        node_lines.append("  d%s  %s" % (depth if depth is not None else "?", label(node)))
    blocks.append((["Nodes"] + (node_lines or ["  none beyond the seeds"]), 2, "nodes"))

    edge_lines = [
        "  %s -%s-> %s" % (e.get("source") or "?", e.get("type") or "?", e.get("target") or "?")
        for e in map(_dict, edges)
    ]
    blocks.append((["Edges"] + (edge_lines or ["  none"]), 1, "edges"))

    if payload.get("truncated"):
        limits = _dict(payload.get("limits"))
        reasons = [str(reason) for reason in _list(payload.get("truncation_reasons")) if reason]
        if not reasons and payload.get("truncation_reason"):
            reasons = [str(payload["truncation_reason"])]
        texts = []
        for reason in reasons:
            template = _SUBGRAPH_CUTS.get(reason)
            try:
                texts.append("%s: %s" % (reason, template.format(**limits)) if template else reason)
            except (KeyError, IndexError, ValueError):
                texts.append(reason)
        blocks.append(([truncated_line(texts)], None))
    return blocks


def _flow_summary_line(flow):
    entry = _dict(_dict(flow).get("entry"))
    line = _flow_head(flow)
    if entry:
        line += "  entry " + (location(entry) or str(entry.get("name") or "?"))
    return line


def render_flows(payload, target):
    mode = payload.get("mode") or "list"
    if mode == "show":
        flow = _dict(payload.get("flow"))
        metrics = _dict(flow.get("metrics"))
        head = ["flow: %s (dataset %s)" % (_flow_head(flow, "", metrics=False), target.dataset)]
        routes = [str(_dict(r).get("name") or "?") for r in _list(flow.get("routes"))]
        if routes:
            head.append("  routes: " + ", ".join(routes))
        facts = []
        for key, text in (
            ("reach", "%d symbols reached"),
            ("modules", "%d modules"),
            ("max_depth", "depth %d"),
            ("unresolved_calls", "%d unresolved calls"),
        ):
            if _int(metrics.get(key)) is not None:
                facts.append(text % metrics[key])
        if facts:
            head.append("  " + ", ".join(facts))
        if flow.get("summary"):
            head.append("  summary: " + _short(flow["summary"], 400))
        blocks = [(head, None)]
        stages = _flow_entry_and_stages(flow, indent="  ", docs=True)
        blocks.append((["Entry and stages"] + (stages or ["  none"]), 2, "stages"))
        modules = [
            "%s (%s)" % (_dict(m).get("name") or "?", _dict(m).get("symbols") or "?")
            for m in _list(flow.get("modules"))
        ]
        tail = []
        if modules:
            tail.append("modules: " + ", ".join(modules))
        for key, text in (("same_reach_as", "same reach as"), ("nested_in", "nested in")):
            names = [str(name) for name in _list(flow.get(key))]
            if names:
                tail.append("%s: %s" % (text, ", ".join(names)))
        if tail:
            blocks.append((tail, 1, "modules and related flows"))
        if metrics.get("truncated"):
            blocks.append(
                ([truncated_line(["the flow is larger than Enola's 5000-symbol walk"])], None)
            )
        return blocks

    flows = _list(payload.get("flows"))
    total = _int(payload.get("total"))
    if mode == "through":
        symbol = _dict(payload.get("symbol"))
        head = "flows through %s: %d (dataset %s)" % (label(symbol), len(flows), target.dataset)
        lines = []
        for flow in flows:
            flow = _dict(flow)
            line = _flow_summary_line(flow)
            via = flow.get("via")
            if via:
                line += "  via %s" % via
            if _int(flow.get("distance")) is not None:
                line += ", distance %d" % flow["distance"]
            lines.append(line)
        blocks = [([head], None), (lines or ["  none"], 1, "flows")]
        if payload.get("truncated"):
            reason = "the reverse call walk stopped early; more flows may pass through"
            blocks.append(([truncated_line([reason])], None))
        return blocks

    shown = len(flows)
    head = "flows: %d of %s (dataset %s)" % (
        shown,
        total if total is not None else shown,
        target.dataset,
    )
    lines = []
    for flow in flows:
        lines.append(_flow_summary_line(flow))
        summary = _dict(flow).get("summary")
        if summary:
            lines.append("       " + _short(summary))
    blocks = [([head], None), (lines or ["  none"], 1, "flows")]
    if payload.get("has_more"):
        reason = "%d of %s flows shown; raise --limit (up to 50)" % (
            shown,
            total if total is not None else "more",
        )
        blocks.append(([truncated_line([reason])], None))
    return blocks


def render_hubs(payload, target):
    hubs = _list(payload.get("hubs"))
    total = _int(payload.get("total"))
    head = "hubs: top %d%s by degree (dataset %s)" % (
        len(hubs),
        " of %d" % total if total is not None else "",
        target.dataset,
    )
    lines = []
    for hub in hubs:
        hub = _dict(hub)
        degrees = "degree %s (in %s, out %s)" % (
            hub.get("degree", "?"),
            hub.get("in_degree", "?"),
            hub.get("out_degree", "?"),
        )
        lines.append("  %s  %s" % (label(hub), degrees))
    return [([head], None), (lines or ["  none"], 1, "hubs")]


def render_capabilities(payload, target):
    features = _dict(payload.get("features"))
    counts = _dict(payload.get("counts"))
    lines = [
        "capabilities (dataset %s)" % target.dataset,
        "  operations: %s" % ", ".join(str(op) for op in _list(payload.get("operations"))),
        "  features: docstrings %s, flows %s"
        % ("yes" if features.get("docs") else "no", "yes" if features.get("flows") else "no"),
    ]
    parts = []
    for key, text in (
        ("nodes", "%s nodes"),
        ("edges", "%s edges"),
        ("symbols", "%s symbols"),
        ("symbols_with_docs", "%s with docstrings"),
        ("flows", "%s flows"),
        ("repositories", "%s repositories"),
    ):
        if counts.get(key) is not None:
            parts.append(text % counts[key])
    if parts:
        lines.append("  counts: " + ", ".join(parts))
    versions = [str(v) for v in _list(payload.get("enola_versions"))]
    if versions:
        lines.append("  enola: " + ", ".join(versions))
    return [(lines, None)]


COMMANDS = {
    "ask": (cmd_ask, render_ask),
    "search": (cmd_search, render_search),
    "neighbors": (cmd_neighbors, render_neighbors),
    "subgraph": (cmd_subgraph, render_subgraph),
    "flows": (cmd_flows, render_flows),
    "hubs": (cmd_hubs, render_hubs),
    "capabilities": (cmd_capabilities, render_capabilities),
}


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """argparse whose usage errors reach the agent: stdout, exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print("Cognee code graph: usage error: %s. Run %s --help." % (message, self.prog))
        self.exit(EXIT_USAGE)


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("%r is not a positive integer" % value)
    return number


def _csv(value):
    items = [item.strip() for item in str(value).split(",") if item.strip()]
    if not items:
        raise argparse.ArgumentTypeError("%r names nothing" % value)
    return items


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--dataset",
        default="",
        help="code dataset name or UUID (default: the repository around the current directory)",
    )
    common.add_argument("--json", action="store_true", help="print the raw operation payload")

    parser = _Parser(
        prog=_HELPER,
        description="Ask Cognee's code graph how the code works. Exit statuses: 0 result, "
        "2 usage, 3 fallback to cognee-code ops, 4 no code graph, 5 unavailable, "
        "6 not found, 7 feature unavailable.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    ask = sub.add_parser("ask", parents=[common], help="seeds, flows and citations for a question")
    ask.add_argument("question")
    ask.add_argument("--max-seeds", type=_positive_int, help="seeds to start from (max 10)")
    ask.add_argument("--include-tests", action="store_true", help="also match test code")

    search = sub.add_parser("search", parents=[common], help="ranked symbols, routes and flows")
    search.add_argument("text")
    search.add_argument("--kinds", type=_csv, help="comma-separated kinds, or all")
    search.add_argument("--limit", type=_positive_int, help="hits to return (max 50)")
    search.add_argument("--include-tests", action="store_true", help="also match test code")

    neighbors = sub.add_parser("neighbors", parents=[common], help="one hop around a node")
    neighbors.add_argument("anchor", metavar="name-or-id")
    neighbors.add_argument("--direction", choices=("both", "forward", "reverse"))
    neighbors.add_argument(
        "--relation", type=_csv, action="extend", help="relation types, e.g. calls (repeatable)"
    )
    neighbors.add_argument("--limit", type=_positive_int, help="neighbours to return (max 200)")

    subgraph = sub.add_parser("subgraph", parents=[common], help="nodes and edges around seeds")
    subgraph.add_argument("seeds", nargs="+", metavar="seed")
    subgraph.add_argument("--depth", type=_positive_int, help="hops from the seeds (1-3)")
    subgraph.add_argument("--max-nodes", type=_positive_int, help="node cap (max 500)")

    flows = sub.add_parser("flows", parents=[common], help="entry points and what they reach")
    flows.add_argument("mode", nargs="?", choices=("list", "show", "through"), default="list")
    flows.add_argument("target", nargs="?", metavar="flow-or-symbol")
    flows.add_argument("--limit", type=_positive_int, help="flows to list (max 50)")

    hubs = sub.add_parser("hubs", parents=[common], help="the most connected symbols")
    hubs.add_argument("--limit", type=_positive_int, help="hubs to return (max 100)")
    hubs.add_argument("--include-tests", action="store_true", help="also count test code")

    sub.add_parser("capabilities", parents=[common], help="operations, features and counts")
    return parser


def _check(parser, args):
    """Argument rules argparse cannot express (exits 2 through ``parser.error``)."""
    # The server reads a blank field as absent and falls back to the query
    # text, which is the operation's name here: ask "" would answer "ask".
    if args.command == "ask" and not args.question.strip():
        parser.error("the question is empty")
    if args.command == "search" and not args.text.strip():
        parser.error("the search text is empty")
    if args.command == "neighbors" and not args.anchor.strip():
        parser.error("the name or id is empty")
    if args.command == "subgraph" and not all(seed.strip() for seed in args.seeds):
        parser.error("a seed is empty")
    if args.command == "flows":
        if args.mode == "list" and args.target:
            parser.error("flows list takes no name; use flows show <flow> or through <symbol>")
        if args.mode != "list" and not (args.target or "").strip():
            what = "a flow name or id" if args.mode == "show" else "a symbol name or id"
            parser.error("flows %s needs %s" % (args.mode, what))
        if args.mode != "list" and args.limit:
            parser.error("--limit applies to flows list only")


def _utf8_stdout():
    """Names and docstrings can be any Unicode; a non-UTF-8 stdout must not crash on them."""
    for stream in (sys.stdout, sys.stderr):
        encoding = str(getattr(stream, "encoding", "") or "").lower()
        if encoding.replace("-", "").replace("_", "") != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv=None):
    _utf8_stdout()
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
        _check(parser, args)
    except SystemExit as exc:  # --help (0) or a usage error (2)
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    command, renderer = COMMANDS[args.command]
    try:
        target = resolve_target(args.dataset)
        payload = command(args, target)
        output = (
            json.dumps(payload, indent=2, ensure_ascii=False)
            if args.json
            else "\n".join(fit(renderer(payload, target)))
        )
    except Stop as stop:
        print("Cognee code graph: " + stop.message)
        return stop.status
    except Exception as exc:  # a bug here must still end in a documented status
        traceback.print_exc()
        print("Cognee code graph: " + unavailable_message("the helper failed: %s" % exc))
        return EXIT_UNAVAILABLE
    print(output)
    return EXIT_OK
