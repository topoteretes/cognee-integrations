"""``cognee-ask-code.py`` — the helper behind the cognee-ask-code skill (SDK-1120).

It runs the code graph's navigation operations (SDK-1119: capabilities,
search, neighbors, subgraph, flows, hubs, ask) through ``POST /api/v1/recall``
with ``scope: ["code"]`` and prints a citation-first answer. The servers here
are shaped exactly as that contract says: recall's envelope (entries with
``source: "code"``, the payload under ``raw`` and its JSON dump under
``text``), the 422 body ``{"detail": "... [CodeSearchValidationError]",
"code", "param"}``, and cognee 1.6.3 (cloud tenants), which answers an
operation it does not know with a 422 and no ``code``. What is pinned:

  * a new server: ``ask`` probes ``capabilities``, then asks, and prints the
    seeds, flows and citations as ``path:line``, one coverage line (docstrings
    on k/n seeds, flows, unresolved calls, truncation), the notes, and the
    next steps with the equivalent helper command;
  * the exit statuses the skill keys on — 3 fallback (an old server, or no
    ``ask`` in capabilities), 4 no code graph, 5 unavailable, 6 not found,
    7 feature unavailable, 2 usage — each one "Cognee code graph:" paragraph
    on stdout;
  * recall turns a seed miss (and a missing graph or feature) into an empty
    result, so an empty navigation result is told apart by one capabilities
    probe;
  * a truncated neighbors or subgraph result ends with ``truncated: yes
    (<reason>)``, and the printed output stays near 120 lines;
  * ``--json`` prints the payload untouched;
  * both scripts compile and run under a real Python 3.9 (the host floor).

Most tests answer at ``_recall_http``'s opener seam — one answer per CODE
operation, every request recorded — so the whole transport runs, 422 parsing
included; the wire tests use the mock server. Claude Code only: the skill
ships with that plugin.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error

import pytest

DATASET = "codebase-shop-1a2b3c4d"
RECALL = "/api/v1/recall"
QUESTION = "How does checkout work in this repo?"

PLACE_ORDER = "0b6f7a52-8c1e-5d2a-9f43-1c7e2b9d4a01"
CHECKOUT_API = "3c2d9e14-7f6b-5a80-b1c2-8d4e6f0a2b02"
PUBLISH = "5e8a1b37-2c94-5f6d-a7e0-4b9c3d1f6e03"
FLOW = "9a7c5e31-4b2d-5c8f-a6e1-2f0d8b4c7a10"


def unsupported(operation):
    """cognee 1.6.3's answer to an operation it does not know: 422, no ``code``."""
    return {
        "detail": "Unsupported CODE operation %r; expected one of architecture, delta, explore, "
        "find_path, impact_analysis, insights, query_facts, traverse. "
        "[CodeSearchValidationError]" % operation
    }


def rejected(code, message, param=None):
    """A 422 body from a server with SDK-1119: a stable code (and param) next to detail."""
    body = {"detail": "%s [CodeSearchValidationError]" % message, "code": code}
    if param:
        body["param"] = param
    return body


def entry(payload):
    """One recall result, as the server wraps a CODE payload."""
    return {
        "kind": "code",
        "search_type": "CODE",
        "text": json.dumps(payload),
        "system_prompt": None,
        "score": None,
        "dataset_id": "6f1d1f7e-3c55-5b1b-9a3e-5a1d0c2e9b10",
        "dataset_name": DATASET,
        "metadata": {},
        "raw": payload,
        "structured": None,
        "source": "code",
    }


ALL_OPERATIONS = sorted(
    [
        "architecture",
        "ask",
        "capabilities",
        "delta",
        "explore",
        "find_path",
        "flows",
        "hubs",
        "impact_analysis",
        "insights",
        "neighbors",
        "query_facts",
        "search",
        "subgraph",
        "traverse",
    ]
)

CAPABILITIES = {
    "operation": "capabilities",
    "version": 1,
    "operations": ALL_OPERATIONS,
    "features": {"docs": True, "flows": True},
    "counts": {
        "nodes": 812,
        "edges": 2304,
        "symbols": 640,
        "symbols_with_docs": 410,
        "flows": 14,
        "repositories": 1,
    },
    "enola_versions": ["0.12.0"],
    "limits": {"ask": {"max_seeds": {"default": 5, "max": 10}}},
}


def capabilities(**changes):
    payload = json.loads(json.dumps(CAPABILITIES))
    for key, value in changes.items():
        payload[key] = value
    return payload


def symbol(node_id, name, file, line, end_line=None, kind="function", doc=None, **extra):
    """A fact as the server's ``fact()`` returns it (relations left out)."""
    fact = {
        "id": node_id,
        "kind": "symbol",
        "type": "CodeSymbol",
        "name": name,
        "file": file,
        "line": line,
        "repo": "shop",
        "symbol_kind": kind,
    }
    if end_line:
        fact["end_line"] = end_line
    if doc:
        fact["doc"] = doc
        fact["description"] = doc
    fact.update(extra)
    return fact


def ref(name, file, line, node_id=None, doc=None, **extra):
    """A flow ref: entry, stage or route."""
    enola_id = hashlib.md5(name.encode("utf-8")).hexdigest()
    out = {"name": name, "enola_id": enola_id, "node_id": node_id, "file": file, "line": line}
    if doc:
        out["doc"] = doc
    out.update(extra)
    return out


SEED_PLACE_ORDER = symbol(
    PLACE_ORDER,
    "shop/checkout.CheckoutService.place_order",
    "shop/checkout.py",
    12,
    58,
    kind="method",
    doc="Place an order: validate the cart, charge the card, then persist the order.",
    score=14.2031,
    matched=["name", "doc"],
)
SEED_CHECKOUT_API = symbol(
    CHECKOUT_API,
    "shop/api.checkout",
    "shop/api.py",
    30,
    41,
    doc="HTTP handler for POST /checkout.",
    score=9.8125,
    matched=["name", "doc", "file"],
)
SEED_PUBLISH = symbol(
    PUBLISH,
    "shop/checkout_events.publish_checkout_completed",
    "shop/checkout_events.py",
    5,
    14,
    score=6.07,
    matched=["name", "file"],
)

CHECKOUT_FLOW = {
    "id": FLOW,
    "enola_id": "57cf2fc2c11a93e5059bbeb64c041d29",
    "name": "POST /checkout",
    "rank": 1,
    "repo": "shop",
    "mechanism": "route",
    "entry": ref("shop/api.checkout", "shop/api.py", 30, CHECKOUT_API, "HTTP handler."),
    "routes": [ref("POST /checkout", "shop/api.py", 28, method="POST")],
    "summary": "The program starts at shop/api.checkout. It hands off to place_order, which "
    "validates the cart and charges the card.",
    "metrics": {"reach": 42, "modules": 5, "max_depth": 4, "unresolved_calls": 4},
    "modules": [{"name": "shop", "symbols": 30}, {"name": "shop/payments", "symbols": 12}],
    "stages": [
        {
            "order": 1,
            "depth": 1,
            "reach": 20,
            **ref("shop/checkout.CheckoutService.place_order", "shop/checkout.py", 12, PLACE_ORDER),
        },
        {
            "order": 2,
            "depth": 2,
            "reach": 6,
            "parent": "e1",
            **ref("shop/cart.Cart.validate", "shop/cart.py", 40, doc="Reject an empty cart."),
        },
        {
            "order": 3,
            "depth": 2,
            "reach": 9,
            "parent": "e1",
            **ref("shop/payments.charge_card", "shop/payments.py", 88),
        },
        {"order": 4, "depth": 1, "reach": 5, **ref("shop/orders.save_order", "shop/orders.py", 7)},
    ],
}

CALLER = "7d3b2a19-6e5c-5b4a-8f3e-9c2d1b0a4f05"
CART = "8e4c3b2a-7f6d-5c5b-9a4f-0d3e2c1b5a06"
ROUTE = "1f5d4c3b-8a7e-5d6c-ab50-1e4f3d2c6b07"

ASK = {
    "operation": "ask",
    "question": QUESTION,
    "terms": ["checkout"],
    "seeds": [SEED_PLACE_ORDER, SEED_CHECKOUT_API, SEED_PUBLISH],
    "flows": [CHECKOUT_FLOW],
    "neighborhood": {
        "nodes": [
            {**SEED_PLACE_ORDER, "role": "seed", "depth": 0},
            {**SEED_CHECKOUT_API, "role": "seed", "depth": 0},
            {
                **symbol(CALLER, "shop/cli.place_order_command", "shop/cli.py", 19, 25),
                "role": "caller",
                "depth": 1,
            },
            {
                **symbol(CART, "shop/cart.Cart.validate", "shop/cart.py", 40, 52, kind="method"),
                "role": "callee",
                "depth": 1,
            },
            {
                "id": ROUTE,
                "kind": "route",
                "type": "ApiEndpoint",
                "name": "POST /checkout",
                "file": "shop/api.py",
                "line": 28,
                "role": "neighbor",
                "depth": 1,
            },
        ],
        "edges": [
            {
                "source_id": CALLER,
                "source": "shop/cli.place_order_command",
                "target_id": PLACE_ORDER,
                "target": "shop/checkout.CheckoutService.place_order",
                "type": "calls",
            },
            {
                "source_id": PLACE_ORDER,
                "source": "shop/checkout.CheckoutService.place_order",
                "target_id": CART,
                "target": "shop/cart.Cart.validate",
                "type": "calls",
            },
            {
                "source_id": ROUTE,
                "source": "POST /checkout",
                "target_id": CHECKOUT_API,
                "target": "shop/api.checkout",
                "type": "handled_by",
            },
            {
                "source_id": CHECKOUT_API,
                "source": "shop/api.checkout",
                "target_id": PLACE_ORDER,
                "target": "shop/checkout.CheckoutService.place_order",
                "type": "calls",
            },
        ],
    },
    "citations": [
        {
            "node_id": PLACE_ORDER,
            "kind": "symbol",
            "name": "shop/checkout.CheckoutService.place_order",
            "file": "shop/checkout.py",
            "line": 12,
            "end_line": 58,
            "cite": "shop/checkout.py:12",
            "why": "seed",
        },
        {
            "node_id": CHECKOUT_API,
            "kind": "symbol",
            "name": "shop/api.checkout",
            "file": "shop/api.py",
            "line": 30,
            "end_line": 41,
            "cite": "shop/api.py:30",
            "why": "seed",
        },
        {
            "node_id": PUBLISH,
            "kind": "symbol",
            "name": "shop/checkout_events.publish_checkout_completed",
            "file": "shop/checkout_events.py",
            "line": 5,
            "end_line": 14,
            "cite": "shop/checkout_events.py:5",
            "why": "seed",
        },
        {
            "node_id": CART,
            "kind": "symbol",
            "name": "shop/cart.Cart.validate",
            "file": "shop/cart.py",
            "line": 40,
            "end_line": 52,
            "cite": "shop/cart.py:40",
            "why": "stage 2 of flow POST /checkout",
        },
        {
            "node_id": "2a6e5d4c-9b8f-5e7d-bc61-2f5a4e3d7c08",
            "kind": "symbol",
            "name": "shop/orders.save_order",
            "file": "shop/orders.py",
            "line": 7,
            "cite": "shop/orders.py:7",
            "why": "stage 4 of flow POST /checkout",
        },
        {
            "node_id": CALLER,
            "kind": "symbol",
            "name": "shop/cli.place_order_command",
            "file": "shop/cli.py",
            "line": 19,
            "end_line": 25,
            "cite": "shop/cli.py:19",
            "why": "calls shop/checkout.CheckoutService.place_order",
        },
    ],
    "coverage": {
        "code_graph": True,
        "docs_present": True,
        "seeds_with_docs": 2,
        "seeds": 3,
        "flows_present": True,
        "unresolved_calls": 4,
        "truncated": True,
        "notes": [
            "4 calls in these flows go to code outside the graph (libraries or callees the "
            "extractor could not resolve).",
            "The neighbourhood was cut at 60 nodes; use subgraph or neighbors to see more.",
        ],
    },
    "next": [
        {
            "code_query": {"operation": "flows", "mode": "show", "id": FLOW},
            "why": "Every stage of POST /checkout.",
        },
        {
            "code_query": {"operation": "neighbors", "id": PLACE_ORDER},
            "why": "Everything one hop from the top seed.",
        },
        {
            "code_query": {
                "operation": "subgraph",
                "seed_ids": [PLACE_ORDER, CHECKOUT_API, PUBLISH],
                "depth": 2,
            },
            "why": "The code two hops around the seeds.",
        },
    ],
    "truncated": True,
}

SEARCH = {
    "operation": "search",
    "query": "validate token",
    "terms": ["valid", "token"],
    "hits": [
        symbol(
            "4b1a2c3d-5e6f-5a7b-8c9d-0e1f2a3b4c09",
            "auth/tokens.validate_token",
            "auth/tokens.py",
            14,
            40,
            doc="Validate a bearer token and return its claims.",
            score=9.5312,
            matched=["name", "doc"],
        ),
        {
            "id": "6d5c4b3a-2f1e-5d0c-9b8a-7f6e5d4c3b10",
            "kind": "flow",
            "type": "CodeFlow",
            "name": "POST /login",
            "file": "auth/api.py",
            "line": 12,
            "repo": "shop",
            "mechanism": "route",
            "rank": 3,
            "summary": "The program starts at auth/api.login.",
            "enola_id": "0f4e3d2c1b0a99887766554433221100",
            "score": 3.25,
            "matched": ["summary"],
        },
    ],
    "total": 2,
    "offset": 0,
    "limit": 10,
    "has_more": False,
}

ANCHOR = symbol(SEARCH["hits"][0]["id"], "auth/tokens.validate_token", "auth/tokens.py", 14, 40)


def neighbors_payload(shown, total):
    rows = [
        {
            "relation": "calls",
            "direction": "in" if i % 2 else "out",
            "node": symbol("n-%d" % i, "auth/mod%d.f%d" % (i, i), "auth/mod%d.py" % i, i + 1),
        }
        for i in range(shown)
    ]
    return {
        "operation": "neighbors",
        "anchor": ANCHOR,
        "neighbors": rows,
        "flows": [{"id": FLOW, "name": "POST /login", "as": "stage"}],
        "total": total,
        "offset": 0,
        "limit": shown,
        "has_more": shown < total,
        "truncated": shown < total,
    }


def subgraph_payload(nodes=3, edges=2, reasons=()):
    seed = {**SEED_PLACE_ORDER, "depth": 0}
    rest = [
        {**symbol("s-%d" % i, "shop/m%d.f" % i, "shop/m%d.py" % i, i + 1), "depth": 1 + i % 2}
        for i in range(nodes - 1)
    ]
    return {
        "operation": "subgraph",
        "seeds": [SEED_PLACE_ORDER],
        "seeds_missing": [
            {
                "seed": "Cart",
                "reason": "ambiguous",
                "candidates": [
                    {
                        "id": "c1",
                        "name": "shop/cart.Cart",
                        "kind": "symbol",
                        "file": "shop/cart.py",
                    },
                    {"id": "c2", "name": "web/cart.Cart", "kind": "symbol", "file": "web/cart.py"},
                ],
            }
        ],
        "nodes": [seed] + rest,
        "edges": [
            {
                "source_id": PLACE_ORDER,
                "source": "shop/checkout.CheckoutService.place_order",
                "target_id": "s-%d" % (i % max(1, nodes - 1)),
                "target": "shop/m%d.f" % (i % max(1, nodes - 1)),
                "type": "calls",
            }
            for i in range(edges)
        ],
        "limits": {"seeds": 20, "depth": 3, "max_nodes": 100, "max_edges": 300},
        "truncated": bool(reasons),
        "truncation_reason": reasons[0] if reasons else None,
        "truncation_reasons": list(reasons),
        "stats": {"nodes_visited": nodes, "edges_considered": edges, "depth_reached": 2},
    }


# ── harness ────────────────────────────────────────────────────────────────


class _Reply:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.data


class FakeServer:
    """A cognee server at ``_recall_http``'s opener seam.

    Answers ``POST /api/v1/recall`` per CODE operation and records every
    request. An answer is a recall result list, ``(status, body)`` for an
    HTTP error, ``bytes`` for a 200 that is not JSON, or an exception to
    raise. Operations with no answer get recall's empty list.
    """

    def __init__(self):
        self.answers = {}
        self.requests = []

    def open(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        self.requests.append(
            {"body": body, "api_key": req.get_header("X-api-key"), "timeout": timeout}
        )
        answer = self.answers.get(body["code_query"]["operation"], [])
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, tuple):
            status, reply = answer
            data = json.dumps(reply).encode("utf-8")
            raise urllib.error.HTTPError(req.full_url, status, "error", {}, io.BytesIO(data))
        return _Reply(answer if isinstance(answer, bytes) else json.dumps(answer).encode("utf-8"))

    @property
    def operations(self):
        return [request["body"]["code_query"]["operation"] for request in self.requests]


@pytest.fixture
def claude(suite):
    if suite.name != "claude-code":
        pytest.skip("cognee-ask-code ships with the Claude Code plugin only")
    return suite


@pytest.fixture
def ask(claude, hook_module, monkeypatch, tmp_path):
    module = hook_module(claude, "_ask_code.py")
    monkeypatch.setenv("COGNEE_BASE_URL", "http://cognee.test")
    monkeypatch.setenv("COGNEE_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)
    return module


@pytest.fixture
def server(ask, monkeypatch):
    fake = FakeServer()
    monkeypatch.setattr(sys.modules["_recall_http"], "_HTTPS_OPENER", fake)
    return fake


def run(ask, capsys, *argv):
    status = ask.main(list(argv))
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def section(out, title):
    """The lines of one titled section, up to the next unindented line."""
    lines = out.splitlines()
    start = lines.index(title) + 1
    end = start
    while end < len(lines) and lines[end].startswith(" "):
        end += 1
    return lines[start:end]


def assert_message(out, *needles):
    assert out.startswith("Cognee code graph: "), out
    assert out.count("\n") == 1, "one paragraph, one line: %r" % out
    for needle in needles:
        assert needle in out, (needle, out)


# ── a server with the new operations ───────────────────────────────────────


def test_ask_prints_seeds_flows_and_citations_as_path_line(ask, server, capsys):
    server.answers = {"capabilities": [entry(CAPABILITIES)], "ask": [entry(ASK)]}

    status, out, _ = run(ask, capsys, "ask", QUESTION, "--dataset", DATASET)

    assert status == ask.EXIT_OK
    # capabilities first, then the question; both scoped to the repo's code dataset
    assert server.operations == ["capabilities", "ask"]
    for request in server.requests:
        body = request["body"]
        assert body["scope"] == ["code"] and body["datasets"] == [DATASET]
        assert body["query"] and "session_id" not in body
        assert request["api_key"] == "test-key"
    assert server.requests[1]["body"]["code_query"] == {"operation": "ask", "question": QUESTION}

    assert out.splitlines()[0] == "ask: %s (dataset %s)" % (QUESTION, DATASET)
    seeds = section(out, "Seeds")
    assert seeds[0] == (
        "  shop/checkout.py:12  shop/checkout.CheckoutService.place_order [method]"
        "  matched name, doc"
    )
    assert seeds[1].strip().startswith("Place an order: validate the cart")
    assert any(
        line.startswith("  shop/checkout_events.py:5  ") and line.endswith("(no docstring)")
        for line in seeds
    )

    flows = section(out, "Flows")
    assert flows[0] == "  POST /checkout  [route, rank 1]  reach 42, 4 unresolved calls"
    assert flows[1] == "    entry  shop/api.py:30  shop/api.checkout"
    assert flows[2] == (
        "      #1  shop/checkout.py:12  shop/checkout.CheckoutService.place_order  (reach 20)"
    )
    assert flows[3] == "        #2  shop/cart.py:40  shop/cart.Cart.validate  (reach 6)"
    assert flows[5] == "      #4  shop/orders.py:7  shop/orders.save_order  (reach 5)"

    hood = section(out, "Neighbourhood (one hop)")
    assert hood == [
        "  caller    shop/cli.py:19  shop/cli.place_order_command [function]"
        "  (calls shop/checkout.CheckoutService.place_order)",
        "  callee    shop/cart.py:40  shop/cart.Cart.validate [method]"
        "  (called by shop/checkout.CheckoutService.place_order)",
        "  neighbor  shop/api.py:28  POST /checkout [route]  (linked to shop/api.checkout by "
        "handled_by)",
    ]

    citations = section(out, "Citations (read these lines before answering)")
    assert (
        citations[0]
        == "  shop/checkout.py:12-58  shop/checkout.CheckoutService.place_order  (seed)"
    )
    assert "  shop/cart.py:40-52  shop/cart.Cart.validate  (stage 2 of flow POST /checkout)" in (
        citations
    )
    assert "  shop/orders.py:7  shop/orders.save_order  (stage 4 of flow POST /checkout)" in (
        citations
    )
    assert len(citations) == len(ASK["citations"])

    coverage = [line for line in out.splitlines() if line.startswith("Coverage:")]
    assert coverage == [
        "Coverage: docs on 2/3 seeds (1 missing); flows present; unresolved calls: 4; "
        "truncated: yes"
    ]
    assert "  - The neighbourhood was cut at 60 nodes; use subgraph or neighbors to see more." in (
        section(out, "Notes")
    )
    nxt = section(out, "Next")
    assert nxt[0] == (
        '  - {"operation": "flows", "mode": "show", "id": "%s"}  Every stage of POST /checkout.'
        % FLOW
    )
    assert "    cognee-ask-code.py flows show %s --dataset %s" % (FLOW, DATASET) in nxt
    assert "    cognee-ask-code.py neighbors %s --dataset %s" % (PLACE_ORDER, DATASET) in nxt
    assert (
        "    cognee-ask-code.py subgraph %s %s %s --depth 2 --dataset %s"
        % (PLACE_ORDER, CHECKOUT_API, PUBLISH, DATASET)
        in nxt
    )
    assert out.splitlines()[-1] == "truncated: yes (neighbourhood cut at 60 nodes)"

    order = [out.index(title) for title in ("Seeds", "Flows", "Neighbourhood", "Citations")]
    order += [out.index(title) for title in ("Coverage:", "Notes", "Next")]
    assert order == sorted(order)


def test_ask_on_a_snapshot_without_docstrings_says_so(ask, server, capsys):
    bare = [
        {k: v for k, v in seed.items() if k not in ("doc", "description")} for seed in ASK["seeds"]
    ]
    payload = {
        **ASK,
        "seeds": bare,
        "flows": [],
        "truncated": False,
        "coverage": {
            **ASK["coverage"],
            "docs_present": False,
            "seeds_with_docs": 0,
            "flows_present": False,
            "unresolved_calls": None,
            "truncated": False,
            "notes": ["This snapshot has no docstrings, so matching used names and paths only."],
        },
    }
    server.answers = {"capabilities": [entry(CAPABILITIES)], "ask": [entry(payload)]}

    status, out, _ = run(ask, capsys, "ask", QUESTION, "--dataset", DATASET)

    assert status == ask.EXIT_OK
    assert (
        "Coverage: docs on 0/3 seeds (3 missing), none in this snapshot; no flows; "
        "unresolved calls: unknown; truncated: no"
    ) in out.splitlines()
    assert "(no docstring)" not in out  # the coverage line says it once for all seeds
    assert section(out, "Flows") == ["  none traced"]
    assert not out.splitlines()[-1].startswith("truncated:")


def test_a_neighbour_of_two_seeds_gives_the_reason_its_citation_gives(ask, server, capsys):
    """The server cites a node for the seed that brought it in; the line must agree."""
    test_ref, audit = "7d3b2a19-6e5c-5b4a-8f3e-9c2d1b0a4f99", "8e4c3b2a-7f6d-5c5b-9a4f-0d3e2c1b5a99"
    names = {
        PLACE_ORDER: SEED_PLACE_ORDER["name"],
        CHECKOUT_API: SEED_CHECKOUT_API["name"],
        test_ref: "tests/test_checkout.test_checkout",
        audit: "shop/audit.record",
    }

    def calls(source, target):
        return {
            "source_id": source,
            "source": names[source],
            "target_id": target,
            "target": names[target],
            "type": "calls",
        }

    hood = {
        "nodes": ASK["neighborhood"]["nodes"][:2]
        + [
            {**symbol(test_ref, names[test_ref], "tests/test_checkout.py", 3), "role": "caller"},
            {**symbol(audit, names[audit], "shop/audit.py", 9), "role": "callee"},
        ],
        # Edge order is the server's stable sort, not the order seeds took turns in.
        "edges": [
            calls(test_ref, PLACE_ORDER),
            calls(test_ref, CHECKOUT_API),
            calls(audit, PLACE_ORDER),
            calls(CHECKOUT_API, audit),
        ],
    }
    cited = {
        "node_id": test_ref,
        "kind": "symbol",
        "name": names[test_ref],
        "file": "tests/test_checkout.py",
        "line": 3,
        "cite": "tests/test_checkout.py:3",
        "why": "calls shop/api.checkout",
    }
    payload = {**ASK, "neighborhood": hood, "citations": [cited]}
    server.answers = {"capabilities": [entry(CAPABILITIES)], "ask": [entry(payload)]}

    status, out, _ = run(ask, capsys, "ask", QUESTION, "--dataset", DATASET)

    assert status == ask.EXIT_OK
    assert section(out, "Neighbourhood (one hop)") == [
        "  caller    tests/test_checkout.py:3  tests/test_checkout.test_checkout [function]"
        "  (calls shop/api.checkout)",
        # not cited: the first edge that fits its role
        "  callee    shop/audit.py:9  shop/audit.record [function]  (called by shop/api.checkout)",
    ]
    citations = section(out, "Citations (read these lines before answering)")
    assert citations == [
        "  tests/test_checkout.py:3  tests/test_checkout.test_checkout  (calls shop/api.checkout)"
    ]


def test_ask_passes_max_seeds_and_include_tests(ask, server, capsys):
    server.answers = {"capabilities": [entry(CAPABILITIES)], "ask": [entry(ASK)]}
    run(ask, capsys, "ask", QUESTION, "--max-seeds", "8", "--include-tests", "--dataset", DATASET)
    assert server.requests[1]["body"]["code_query"] == {
        "operation": "ask",
        "question": QUESTION,
        "max_seeds": 8,
        "include_tests": True,
    }


def test_json_prints_the_payload_untouched(ask, server, capsys):
    server.answers = {"capabilities": [entry(CAPABILITIES)], "ask": [entry(ASK)]}
    status, out, _ = run(ask, capsys, "ask", QUESTION, "--dataset", DATASET, "--json")
    assert status == ask.EXIT_OK
    assert json.loads(out) == ASK
    assert server.operations == ["capabilities", "ask"]


def test_payload_is_read_from_text_when_raw_is_not_the_payload(ask, server, capsys):
    """``raw`` is the payload for a dict; anything else leaves only ``text``."""
    wrapped = {**entry(SEARCH), "raw": {"value": "not the payload"}}
    server.answers = {"search": [wrapped]}
    status, out, _ = run(ask, capsys, "search", "validate token", "--dataset", DATASET, "--json")
    assert status == ask.EXIT_OK and json.loads(out) == SEARCH


def test_search_prints_hits_as_path_line(ask, server, capsys):
    server.answers = {"search": [entry(SEARCH)]}
    status, out, _ = run(
        ask, capsys, "search", "validate token", "--kinds", "symbol,flow", "--limit", "5"
    )
    assert status == ask.EXIT_OK
    assert server.requests[0]["body"]["code_query"] == {
        "operation": "search",
        "text": "validate token",
        "kinds": ["symbol", "flow"],
        "limit": 5,
    }
    lines = out.splitlines()
    assert lines[1] == "terms: valid, token"
    assert lines[2] == (
        "  auth/tokens.py:14  auth/tokens.validate_token [function]  score 9.5312"
        "  matched name, doc"
    )
    assert "  auth/api.py:12  POST /login [flow] rank 3  score 3.25  matched summary" in lines
    assert "truncated:" not in out

    server.answers = {"search": [entry({**SEARCH, "total": 23, "has_more": True})]}
    status, out, _ = run(ask, capsys, "search", "validate token")
    assert out.splitlines()[-1] == (
        "truncated: yes (2 of 23 hits shown; raise --limit (up to 50) or narrow --kinds)"
    )


def test_flows_show_and_through(ask, server, capsys):
    server.answers = {
        "flows": [entry({"operation": "flows", "mode": "show", "flow": CHECKOUT_FLOW})]
    }
    status, out, _ = run(ask, capsys, "flows", "show", "POST /checkout", "--dataset", DATASET)
    assert status == ask.EXIT_OK
    assert server.requests[0]["body"]["code_query"] == {
        "operation": "flows",
        "mode": "show",
        "name": "POST /checkout",
    }
    lines = out.splitlines()
    assert lines[0] == "flow: POST /checkout  [route, rank 1] (dataset %s)" % DATASET
    assert lines[2] == "  42 symbols reached, 5 modules, depth 4, 4 unresolved calls"
    assert "  entry  shop/api.py:30  shop/api.checkout" in out
    assert "      #2  shop/cart.py:40  shop/cart.Cart.validate  (reach 6)" in out
    assert "          Reject an empty cart." in out

    through = {
        "operation": "flows",
        "mode": "through",
        "symbol": symbol("p1", "shop/payments.charge_card", "shop/payments.py", 88),
        "flows": [{**CHECKOUT_FLOW, "via": "stage", "distance": 2}],
        "total": 1,
        "truncated": True,
    }
    server.answers = {"flows": [entry(through)]}
    status, out, _ = run(ask, capsys, "flows", "through", PLACE_ORDER, "--dataset", DATASET)
    assert status == ask.EXIT_OK
    assert server.requests[-1]["body"]["code_query"] == {
        "operation": "flows",
        "mode": "through",
        "id": PLACE_ORDER,
    }
    assert "entry shop/api.py:30  via stage, distance 2" in out
    assert out.splitlines()[-1].startswith("truncated: yes (the reverse call walk stopped early")


def test_hubs_and_capabilities_render(ask, server, capsys):
    hub = {**SEED_PLACE_ORDER, "degree": 34, "in_degree": 20, "out_degree": 14}
    server.answers = {
        "hubs": [entry({"operation": "hubs", "hubs": [hub], "total": 640, "limit": 20})],
        "capabilities": [entry(CAPABILITIES)],
    }
    status, out, _ = run(ask, capsys, "hubs", "--dataset", DATASET)
    assert status == ask.EXIT_OK
    assert (
        "  shop/checkout.py:12  shop/checkout.CheckoutService.place_order [method]"
        "  degree 34 (in 20, out 14)"
    ) in out

    status, out, _ = run(ask, capsys, "capabilities", "--dataset", DATASET)
    assert status == ask.EXIT_OK
    assert "  features: docstrings yes, flows yes" in out
    assert (
        "  counts: 812 nodes, 2304 edges, 640 symbols, 410 with docstrings, 14 flows,"
        " 1 repositories"
    ) in out


# ── truncation ─────────────────────────────────────────────────────────────


def test_truncated_neighbors_end_with_the_reason(ask, server, capsys):
    server.answers = {"neighbors": [entry(neighbors_payload(3, 57))]}
    status, out, _ = run(
        ask, capsys, "neighbors", "validate_token", "--relation", "calls", "--direction", "reverse"
    )
    assert status == ask.EXIT_OK
    assert server.requests[0]["body"]["code_query"] == {
        "operation": "neighbors",
        "name": "validate_token",
        "direction": "reverse",
        "relation_types": ["calls"],
    }
    lines = out.splitlines()
    assert lines[0].startswith("neighbors of auth/tokens.py:14  auth/tokens.validate_token")
    assert lines[1] == "  -> calls  auth/mod0.py:1  auth/mod0.f0 [function]"
    assert lines[2] == "  <- calls  auth/mod1.py:2  auth/mod1.f1 [function]"
    assert "in flows: POST /login (stage)" in lines
    assert lines[-1] == (
        "truncated: yes (3 of 57 neighbours shown; raise --limit (up to 200) or use --relation)"
    )

    server.answers = {"neighbors": [entry(neighbors_payload(2, 2))]}
    status, out, _ = run(ask, capsys, "neighbors", "validate_token")
    assert status == ask.EXIT_OK and "truncated:" not in out


def test_truncated_subgraph_names_every_reason_and_the_missing_seeds(ask, server, capsys):
    payload = subgraph_payload(reasons=("max_depth", "max_nodes"))
    server.answers = {"subgraph": [entry(payload)]}
    status, out, _ = run(
        ask, capsys, "subgraph", "place_order", "Cart", PLACE_ORDER, "--depth", "5"
    )
    assert status == ask.EXIT_OK
    assert server.requests[0]["body"]["code_query"] == {
        "operation": "subgraph",
        "seeds": ["place_order", "Cart"],
        "seed_ids": [PLACE_ORDER],
        "depth": 5,
    }
    assert section(out, "Missing seeds") == [
        "  Cart: ambiguous; candidates: shop/cart.Cart [symbol] shop/cart.py, "
        "web/cart.Cart [symbol] web/cart.py"
    ]
    assert "  d1  shop/m0.py:1  shop/m0.f [function]" in section(out, "Nodes")
    assert "  shop/checkout.CheckoutService.place_order -calls-> shop/m0.f" in section(out, "Edges")
    assert out.splitlines()[-1] == (
        "truncated: yes (max_depth: depth capped at 3; max_nodes: stopped at 100 nodes)"
    )


def test_output_stays_near_120_lines_and_keeps_the_truncation_line(ask, server, capsys):
    server.answers = {"subgraph": [entry(subgraph_payload(400, 900, reasons=("max_edges",)))]}
    status, out, _ = run(ask, capsys, "subgraph", "place_order")
    lines = out.splitlines()
    assert status == ask.EXIT_OK
    assert len(lines) <= ask.MAX_LINES
    assert any("more lines not shown; --json prints the whole result" in line for line in lines)
    assert "Missing seeds" in lines  # cut last: it names what was not found
    # The cap's own cut joins the server's reason on the one truncation line.
    assert lines[-1] == (
        "truncated: yes (max_edges: stopped at 300 edges; output capped at 120 lines: nodes, "
        "edges partly hidden; rerun with --json or narrow the query)"
    )


def test_a_cut_only_the_output_cap_made_is_reported_as_truncation(ask, server, capsys):
    """The server cut nothing, but lines the agent never sees are not coverage."""
    hood = {
        "nodes": ASK["neighborhood"]["nodes"][:2]
        + [
            {**symbol("h-%d" % i, "shop/h%d.f" % i, "shop/h%d.py" % i, i + 1), "role": "callee"}
            for i in range(150)
        ],
        "edges": [],
    }
    coverage = {**ASK["coverage"], "truncated": False, "notes": ASK["coverage"]["notes"][:1]}
    payload = {**ASK, "neighborhood": hood, "coverage": coverage, "truncated": False}
    server.answers = {"capabilities": [entry(CAPABILITIES)], "ask": [entry(payload)]}

    status, out, _ = run(ask, capsys, "ask", QUESTION, "--dataset", DATASET)

    lines = out.splitlines()
    assert status == ask.EXIT_OK
    assert len(lines) <= ask.MAX_LINES
    assert section(out, "Neighbourhood (one hop)")[-1].startswith("  ... ")
    assert len(section(out, "Citations (read these lines before answering)")) == 6  # whole
    assert (
        "Coverage: docs on 2/3 seeds (1 missing); flows present; unresolved calls: 4; "
        "truncated: yes"
    ) in lines
    assert lines[-1] == (
        "truncated: yes (output capped at 120 lines: neighbourhood partly hidden; "
        "rerun with --json or narrow the query)"
    )

    # A listing the server sent whole gains the line too.
    server.answers = {"neighbors": [entry(neighbors_payload(150, 150))]}
    status, out, _ = run(ask, capsys, "neighbors", "validate_token", "--dataset", DATASET)
    lines = out.splitlines()
    assert status == ask.EXIT_OK and len(lines) <= ask.MAX_LINES
    assert lines[-1].startswith("truncated: yes (output capped at 120 lines: neighbours partly")

    # Output under the cap is printed whole and says nothing about a cut.
    server.answers = {"neighbors": [entry(neighbors_payload(20, 20))]}
    status, out, _ = run(ask, capsys, "neighbors", "validate_token", "--dataset", DATASET)
    assert status == ask.EXIT_OK
    assert "truncated:" not in out and "not shown" not in out


# ── fallback, not found, no graph, no feature ──────────────────────────────


@pytest.mark.parametrize(
    "rejection",
    [
        unsupported("capabilities"),  # cognee 1.6.3: no code field
        # the stable code alone decides, whatever the message says
        rejected("unknown_operation", "CODE cannot run 'capabilities' here.", "operation"),
        {"detail": "Unknown operation 'capabilities'"},
    ],
    ids=["old-server", "unknown_operation", "unknown-operation-text"],
)
def test_a_server_without_the_operations_is_the_fallback(ask, server, capsys, rejection):
    server.answers = {"capabilities": (422, rejection)}
    status, out, _ = run(ask, capsys, "ask", QUESTION, "--dataset", DATASET)
    assert status == ask.EXIT_FALLBACK == 3
    assert server.operations == ["capabilities"]  # never asks a server that cannot answer
    assert_message(
        out,
        "does not have the ask-the-code operations",
        # named: cognee-search.sh --code resolves only a repository indexed by path
        "cognee-search.sh --code --dataset %s with query_facts" % DATASET,
        "explore",
        "traverse",
        "impact_analysis",
        "grep",
        "read the files",
    )


def test_old_server_rejecting_a_navigation_command_is_the_fallback(ask, server, capsys):
    server.answers = {"neighbors": (422, unsupported("neighbors"))}
    status, out, _ = run(ask, capsys, "neighbors", "validate_token", "--dataset", DATASET)
    assert status == ask.EXIT_FALLBACK
    assert_message(out, "it rejected neighbors", "expected one of architecture")


def test_capabilities_without_ask_is_the_fallback(ask, server, capsys):
    operations = [op for op in ALL_OPERATIONS if op != "ask"]
    server.answers = {"capabilities": [entry(capabilities(operations=operations))]}
    status, out, _ = run(ask, capsys, "ask", QUESTION, "--dataset", DATASET)
    assert status == ask.EXIT_FALLBACK
    assert server.operations == ["capabilities"]
    assert_message(out, "its capabilities list no ask operation", "query_facts")


@pytest.mark.parametrize(
    "answers, sent",
    [
        ({"capabilities": []}, ["capabilities"]),
        (
            {"capabilities": [entry(capabilities(counts={**CAPABILITIES["counts"], "nodes": 0}))]},
            ["capabilities"],
        ),
        (
            {"capabilities": (404, {"detail": "No datasets found. [DatasetNotFoundError]"})},
            ["capabilities"],
        ),
        ({"capabilities": [entry(CAPABILITIES)], "ask": []}, ["capabilities", "ask"]),
        (
            {
                "capabilities": [entry(CAPABILITIES)],
                "ask": (422, rejected("no_code_graph", "CODE found no code graph here.")),
            },
            ["capabilities", "ask"],
        ),
    ],
    ids=["empty-capabilities", "zero-nodes", "http-404", "empty-ask", "no_code_graph"],
)
def test_no_code_graph(ask, server, capsys, answers, sent):
    server.answers = answers
    status, out, _ = run(ask, capsys, "ask", QUESTION, "--dataset", DATASET)
    assert status == ask.EXIT_NO_CODE_GRAPH == 4
    assert server.operations == sent  # the probe stops before asking an empty graph
    assert_message(out, "dataset %s has no code graph" % DATASET, "cognee-index-repo.sh", "grep")


def test_no_code_graph_names_the_repositories_indexed_by_git_url(ask, server, capsys):
    """Indexing such a repository again rewrites the same dataset, so exit 4 names it."""
    cg = sys.modules["_code_graph"]
    for spec, dataset, indexed_at in (
        ("https://github.com/acme/billing", "codebase-billing-0d9e8f7a", 1.0),
        ("https://github.com/acme/shop", "codebase-shop-b1c397fd", 2.0),
        ("/src/notes", "codebase-notes-11aa22bb", 3.0),
    ):
        url = spec.startswith("https://")
        cg.save_repo_state(
            {
                "spec": spec,
                "spec_kind": "url" if url else "path",
                "repo_root": "" if url else spec,
                "dataset": dataset,
                "last_index_at": indexed_at,
            }
        )
    server.answers = {"capabilities": []}

    status, out, _ = run(ask, capsys, "ask", QUESTION, "--dataset", DATASET)

    assert status == ask.EXIT_NO_CODE_GRAPH
    assert_message(
        out,
        "Repositories indexed by git URL from this machine: https://github.com/acme/shop "
        "(dataset codebase-shop-b1c397fd), https://github.com/acme/billing (dataset "
        "codebase-billing-0d9e8f7a). If one of them is this repository, rerun with --dataset "
        "<its dataset> instead of indexing it again; otherwise index the repository first",
        "cognee-index-repo.sh",
    )
    assert "codebase-notes" not in out  # a repository indexed by path is found by its path


def test_an_empty_navigation_result_on_a_graph_is_not_found(ask, server, capsys):
    """Recall turns a seed the graph cannot resolve into an empty list.

    An ambiguous name is just as empty, so the message names both readings.
    """
    server.answers = {"neighbors": [], "capabilities": [entry(CAPABILITIES)]}
    status, out, _ = run(ask, capsys, "neighbors", "NoSuchThing", "--dataset", DATASET)
    assert status == ask.EXIT_NOT_FOUND == 6
    # the search for candidates found nothing either
    assert server.operations == ["neighbors", "capabilities", "search"]
    assert_message(out, 'matched "NoSuchThing", or more than one did', "search", "grep")


def test_a_name_lookup_that_found_nothing_lists_search_candidates(ask, server, capsys):
    """Recall drops the server's candidates with the miss; one search stands in for them."""
    server.answers = {
        "neighbors": [],
        "capabilities": [entry(CAPABILITIES)],
        "search": [entry(SEARCH)],
    }
    status, out, _ = run(ask, capsys, "neighbors", "token", "--dataset", DATASET)
    assert status == ask.EXIT_NOT_FOUND
    assert server.requests[-1]["body"]["code_query"] == {
        "operation": "search",
        "text": "token",
        "kinds": ["symbol", "route", "module", "storage", "service"],
        "include_tests": True,
        "limit": 5,
    }
    assert_message(
        out,
        'matched "token", or more than one did. Search candidates: auth/tokens.validate_token '
        "(auth/tokens.py:14, id %s)" % SEARCH["hits"][0]["id"],
        "Rerun with one of their full names or ids",
    )

    # flows show looks for flows; a failed search leaves the plain message
    server.answers = {
        "flows": [],
        "capabilities": [entry(CAPABILITIES)],
        "search": (500, {"detail": "boom"}),
    }
    status, out, _ = run(ask, capsys, "flows", "show", "POST /cart", "--dataset", DATASET)
    assert status == ask.EXIT_NOT_FOUND
    assert server.requests[-1]["body"]["code_query"]["kinds"] == ["flow"]
    assert_message(out, 'a flow named "POST /cart"', "grep for the name")

    # an id is not searched for
    server.requests.clear()
    server.answers = {"neighbors": [], "capabilities": [entry(CAPABILITIES)]}
    status, out, _ = run(ask, capsys, "neighbors", PLACE_ORDER, "--dataset", DATASET)
    assert status == ask.EXIT_NOT_FOUND
    assert server.operations == ["neighbors", "capabilities"]


def test_an_empty_navigation_result_without_a_graph_is_no_code_graph(ask, server, capsys):
    server.answers = {"subgraph": [], "capabilities": []}
    status, out, _ = run(ask, capsys, "subgraph", "place_order", "--dataset", DATASET)
    assert status == ask.EXIT_NO_CODE_GRAPH
    assert_message(out, "has no code graph")


def test_an_empty_lookup_whose_probe_says_no_code_graph_is_no_code_graph(ask, server, capsys):
    """A definite no_code_graph from the capabilities probe is not a seed miss."""
    server.answers = {
        "neighbors": [],
        "capabilities": (
            422,
            {
                "detail": "This dataset has no code graph. [CodeSearchValidationError]",
                "code": "no_code_graph",
            },
        ),
    }
    status, out, _ = run(ask, capsys, "neighbors", "place_order", "--dataset", DATASET)
    assert status == ask.EXIT_NO_CODE_GRAPH == 4
    assert server.operations == ["neighbors", "capabilities"]
    assert_message(out, "has no code graph")


def test_the_candidate_search_gets_a_short_timeout(ask, server, monkeypatch, capsys):
    """A best-effort search must not hold an empty lookup for a full request timeout."""
    server.answers = {
        "neighbors": [],
        "capabilities": [entry(CAPABILITIES)],
        "search": [entry(SEARCH)],
    }
    status, _, _ = run(ask, capsys, "neighbors", "token", "--dataset", DATASET)
    assert status == ask.EXIT_NOT_FOUND
    assert [request["timeout"] for request in server.requests] == [45.0, 45.0, 10.0]

    # a shorter request timeout also bounds the search
    server.requests.clear()
    monkeypatch.setenv("COGNEE_ASK_CODE_TIMEOUT", "7")
    status, _, _ = run(ask, capsys, "neighbors", "token", "--dataset", DATASET)
    assert status == ask.EXIT_NOT_FOUND
    assert [request["timeout"] for request in server.requests] == [7.0, 7.0, 7.0]


def test_flows_on_a_snapshot_without_flows_is_feature_unavailable(ask, server, capsys):
    """recall softens feature_unavailable to an empty list; capabilities says why."""
    no_flows = capabilities(features={"docs": True, "flows": False})
    server.answers = {"flows": [], "capabilities": [entry(no_flows)]}
    status, out, _ = run(ask, capsys, "flows", "--dataset", DATASET)
    assert status == ask.EXIT_FEATURE_UNAVAILABLE == 7
    assert server.requests[0]["body"]["code_query"] == {"operation": "flows", "mode": "list"}
    assert_message(out, "has no flows", "flows.json", "Skip flows")


@pytest.mark.parametrize(
    "code, message, status_name",
    [
        (
            "ambiguous",
            "CODE node name 'Cart' is ambiguous; use an exact id or repo. Candidates: "
            "shop/cart.Cart [CodeSymbol, repo=shop, id=c1], "
            "web/cart.Cart [CodeSymbol, repo=shop, id=c2]",
            "EXIT_NOT_FOUND",
        ),
        ("not_found", "CODE could not resolve node name 'Cart'.", "EXIT_NOT_FOUND"),
        (
            "feature_unavailable",
            "This snapshot has no flows; they come from Enola's flows.json.",
            "EXIT_FEATURE_UNAVAILABLE",
        ),
        ("invalid_argument", "CODE limit must be an integer.", "EXIT_USAGE"),
    ],
    ids=["ambiguous", "not_found", "feature_unavailable", "invalid_argument"],
)
def test_stable_codes_map_to_statuses(ask, server, capsys, code, message, status_name):
    server.answers = {"neighbors": (422, rejected(code, message, "name"))}
    status, out, _ = run(ask, capsys, "neighbors", "Cart", "--dataset", DATASET)
    assert status == getattr(ask, status_name)
    assert_message(out, message)  # the server's reason, candidates included


# ── unavailable ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "answer, needle",
    [
        (TimeoutError("timed out"), "no answer from http://cognee.test"),
        ((500, {"detail": "boom"}), "HTTP 500"),
        ((503, {"detail": "maintenance"}), "HTTP 503"),
        ((401, {"detail": "Unauthorized"}), "unauthorized (HTTP 401)"),
        ((403, {"detail": "Forbidden"}), "unauthorized (HTTP 403)"),
        (b"<html>not json", "malformed JSON"),
        ([{"source": "code", "text": "plain words", "raw": {"value": "x"}}], "no operation"),
    ],
    ids=["timeout", "500", "503", "401", "403", "malformed", "no-payload"],
)
def test_unavailable(ask, server, capsys, answer, needle):
    server.answers = {"capabilities": answer}
    status, out, _ = run(ask, capsys, "ask", QUESTION, "--dataset", DATASET)
    assert status == ask.EXIT_UNAVAILABLE == 5
    assert_message(out, needle, "say the code graph was not used")


def test_refused_connection_is_unavailable(ask, closed_port_url, monkeypatch, capsys):
    monkeypatch.setenv("COGNEE_BASE_URL", closed_port_url)
    status, out, _ = run(ask, capsys, "capabilities", "--dataset", DATASET)
    assert status == ask.EXIT_UNAVAILABLE
    assert_message(out, "nothing answers at %s" % closed_port_url)


def test_request_timeout_defaults_to_45s_and_follows_the_env(ask, server, monkeypatch, capsys):
    server.answers = {"capabilities": [entry(CAPABILITIES)]}
    run(ask, capsys, "capabilities", "--dataset", DATASET)
    monkeypatch.setenv("COGNEE_ASK_CODE_TIMEOUT", "7")
    run(ask, capsys, "capabilities", "--dataset", DATASET)
    assert [request["timeout"] for request in server.requests] == [45.0, 7.0]


# ── arguments and configuration ────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv, needle",
    [
        ([], "required: command"),
        (["subgraph", "x", "--depth", "0"], "is not a positive integer"),
        (["flows", "show"], "flows show needs a flow name or id"),
        (["flows", "list", "POST /checkout"], "flows list takes no name"),
        (["neighbors", "x", "--direction", "up"], "invalid choice"),
        (["search"], "required: text"),
        # The server would read a blank value as absent and use the query text,
        # which is the operation's name: ask "" would answer the question "ask".
        (["ask", ""], "the question is empty"),
        (["search", "  "], "the search text is empty"),
        (["neighbors", ""], "the name or id is empty"),
        (["subgraph", "place_order", " "], "a seed is empty"),
        (["flows", "through", " "], "flows through needs a symbol name or id"),
    ],
    ids=[
        "no-command",
        "depth-0",
        "show-no-name",
        "list-with-name",
        "bad-direction",
        "no-text",
        "blank-question",
        "blank-text",
        "blank-anchor",
        "blank-seed",
        "blank-symbol",
    ],
)
def test_usage_errors_exit_2_on_stdout(ask, server, capsys, argv, needle):
    status, out, _ = run(ask, capsys, *argv)
    assert status == ask.EXIT_USAGE == 2
    assert_message(out, "usage error", needle)
    assert server.requests == []


def test_help_exits_0(ask, capsys):
    status, out, _ = run(ask, capsys, "--help")
    assert status == 0 and "6 not found" in out


def test_dataset_is_the_indexed_repo_around_the_cwd(ask, server, tmp_path, monkeypatch, capsys):
    cg = sys.modules["_code_graph"]
    repo = tmp_path / "shop"
    (repo / "src").mkdir(parents=True)
    cg.save_repo_state(
        {"spec": str(repo), "spec_kind": "path", "repo_root": str(repo), "dataset": DATASET}
    )
    monkeypatch.chdir(repo / "src")
    server.answers = {"capabilities": [entry(CAPABILITIES)]}

    run(ask, capsys, "capabilities")
    assert server.requests[-1]["body"]["datasets"] == [DATASET]

    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    run(ask, capsys, "capabilities")
    assert server.requests[-1]["body"]["datasets"] == [cg.default_code_dataset(str(elsewhere))]

    dataset_id = "11111111-2222-5333-8444-555555555555"
    run(ask, capsys, "capabilities", "--dataset", dataset_id)
    assert server.requests[-1]["body"]["dataset_ids"] == [dataset_id]
    assert "datasets" not in server.requests[-1]["body"]


def _checkout(path, **remotes):
    """A git checkout with these remotes; no commit, since only the remotes are read."""
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    for name, url in remotes.items():
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", name, url], check=True, capture_output=True
        )
    return path


def test_a_repository_indexed_by_git_url_is_found_through_a_remote(
    ask, server, tmp_path, monkeypatch, capsys
):
    """A cloud server indexes by URL only, so no recorded path matches the checkout."""
    cg = sys.modules["_code_graph"]
    cg.save_repo_state(
        {
            "spec": "https://github.com/acme/shop",
            "spec_kind": "url",
            "repo_root": "",
            "dataset": "codebase-shop-b1c397fd",
        }
    )
    server.answers = {"capabilities": [entry(CAPABILITIES)]}
    shop = _checkout(
        tmp_path / "work" / "shop",
        upstream="https://git.example.com/mirror/shop.git",
        origin="git@github.com:Acme/shop.git",
    )
    (shop / "src").mkdir()
    monkeypatch.chdir(shop / "src")

    status, _, _ = run(ask, capsys, "capabilities")
    assert status == ask.EXIT_OK
    assert server.requests[-1]["body"]["datasets"] == ["codebase-shop-b1c397fd"]

    # A fork is another repository: it gets its own name, and exit 4 names the URL.
    fork = _checkout(tmp_path / "work" / "fork", origin="git@github.com:someone/shop.git")
    monkeypatch.chdir(fork)
    server.answers = {"capabilities": []}
    status, out, _ = run(ask, capsys, "capabilities")
    assert server.requests[-1]["body"]["datasets"] == [
        cg.default_code_dataset(cg.git_repo_root(str(fork)))
    ]
    assert status == ask.EXIT_NO_CODE_GRAPH
    assert_message(
        out, "https://github.com/acme/shop (dataset codebase-shop-b1c397fd)", "rerun with --dataset"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/shop",
        "https://github.com/Acme/shop.git",
        "https://user:token@github.com/acme/shop/",
        "git@github.com:acme/shop.git",
        "ssh://git@github.com:22/acme/shop.git",
        "git://github.com/acme/shop",
    ],
)
def test_every_spelling_of_one_remote_is_one_repository(ask, url):
    assert ask.repo_key(url) == "github.com/acme/shop"
    assert ask.repo_key("/srv/git/shop.git") == ask.repo_key("file:///srv/git/shop") == ""


# ── over the wire ──────────────────────────────────────────────────────────


def test_capabilities_on_the_wire(ask, mock_server, monkeypatch, capsys):
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    mock_server.set_recall_results([entry(CAPABILITIES)])

    status, out, _ = run(ask, capsys, "capabilities", "--dataset", DATASET, "--json")

    assert status == ask.EXIT_OK and json.loads(out) == CAPABILITIES
    call = mock_server.assert_called("POST", RECALL)
    assert call["json"]["scope"] == ["code"]
    assert call["json"]["code_query"] == {"operation": "capabilities"}
    assert call["json"]["datasets"] == [DATASET]
    assert call["headers"].get("X-Api-Key") == "test-key"


def test_old_server_on_the_wire(ask, mock_server, monkeypatch, capsys):
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    mock_server.force_response("POST", RECALL, 422, unsupported("capabilities"))
    status, out, _ = run(ask, capsys, "ask", QUESTION, "--dataset", DATASET)
    assert status == ask.EXIT_FALLBACK
    assert_message(out, "Unsupported CODE operation 'capabilities'", "query_facts", "grep")


def test_new_server_rejection_on_the_wire(ask, mock_server, monkeypatch, capsys):
    """The stable code arrives through _recall_http's envelope, not message text."""
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    body = rejected("feature_unavailable", "Flows need Enola's flows.json.", "operation")
    mock_server.force_response("POST", RECALL, 422, body)
    status, out, _ = run(ask, capsys, "flows", "--dataset", DATASET)
    assert status == ask.EXIT_FEATURE_UNAVAILABLE
    assert_message(out, "has no flows")


def test_missing_dataset_on_the_wire_is_no_code_graph(ask, mock_server, monkeypatch, capsys):
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    mock_server.force_response(
        "POST", RECALL, 404, {"detail": "No datasets found. [DatasetNotFoundError]"}
    )
    status, out, err = run(ask, capsys, "neighbors", "place_order", "--dataset", DATASET)
    assert status == ask.EXIT_NO_CODE_GRAPH
    assert len([c for c in mock_server.calls if c["path"] == RECALL]) == 2  # + the probe
    assert "HTTP 404" in err  # diagnostics stay on stderr


# ── Python 3.9 ─────────────────────────────────────────────────────────────


def _python39():
    """A Python 3.9 on this machine, or '' (the host floor the scripts must run on)."""
    candidates = [shutil.which("python3.9") or ""]
    if sys.platform == "darwin":
        candidates += ["/usr/bin/python3", "/Library/Developer/CommandLineTools/usr/bin/python3"]
    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "import sys; print(sys.version_info[:2] == (3, 9))"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "True":
            return candidate
    return ""


_PY39 = _python39()


@pytest.mark.skipif(not _PY39, reason="no Python 3.9 interpreter on this machine")
def test_scripts_compile_and_run_under_python39(claude, mock_server, run_hook):
    scripts = [str(claude.scripts_dir / name) for name in ("_ask_code.py", "cognee-ask-code.py")]
    compiled = subprocess.run(
        [_PY39, "-c", "import sys\nfor p in sys.argv[1:]: compile(open(p).read(), p, 'exec')"]
        + scripts,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert compiled.returncode == 0, compiled.stderr

    mock_server.set_recall_results([entry(SEARCH)])
    result = run_hook(
        claude,
        "cognee-ask-code.py",
        "search",
        "validate token",
        "--dataset",
        DATASET,
        service_url=mock_server.url,
        python=_PY39,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "  auth/tokens.py:14  auth/tokens.validate_token [function]" in result.stdout
