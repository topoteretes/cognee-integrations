# SaaS Entitlements Copilot - Multi-Agent Demo

This demo showcases specialized AI agents for a SaaS subscription and entitlements platform using **cognee-integration-langgraph**.

## Overview

- **Multi-tenant** (Acme tenant in the demo)
- **Multiple data sources**: billing, subscriptions, entitlements, support tickets, contracts, audit events, plus agent-private datasets
- **User isolation**: each agent (Billing, Support, Entitlements) runs as a separate Cognee user; a supervisor user gets shared read access to all datasets (`ENABLE_BACKEND_ACCESS_CONTROL=True`)
- **Specialized agents**: Billing, Support, Entitlements -- each with role-specific datasets, session-based context, and Cognee search (graph completion)
- **Supervisor** (LangGraph): runs Billing -> Support -> Entitlements -> then root cause + recommendation per incident
- **Two incidents**: TICK-1001 (customer paid but downgraded), TICK-1002 (similar case; reuses TICK-1001 resolution from memory)

## What you'll see (takeaway)

When you run the demo, the script runs the **supervisor once for TICK-1001, then for TICK-1002**. For each incident you get:

- **Billing**: two Cognee queries in the same session (invoice, then billing account) -- context preserved between them.
- **Support** and **Entitlements**: one search each over their datasets.
- **One TEMPORAL timeline** per incident (audit events in order -- no manual merge).
- **Memory-driven resolution**: Each incident's root cause and recommendation are stored in the `agent_resolutions` dataset; TICK-1002's resolution reuses TICK-1001's from Cognee.
- **User isolation proof**: after the main demo, a proof section shows that Billing user cannot access Support data, but Supervisor can (shared read).
- **NodeSet filtering proof**: same query filtered by different NodeSet tags (e.g. "invoices" vs "support") returns results from different subgraphs.

So in one run you see: **one graph** (all agents query it), **one timeline** (single TEMPORAL search), **session context** (Billing's two queries), **memory** (TICK-1002 reusing TICK-1001), **user isolation** (each agent scoped to its own datasets), and **NodeSet filtering** (topic-based subgraph scoping).


## Data sources (NodeSets)

NodeSets are tags you attach to data when ingesting (`node_set=[...]` on `cognee.add()`) and use to filter at search time (`node_type=NodeSet`, `node_name=["invoices"]`), so the same query can target different subgraphs (e.g. only invoice nodes vs only support-ticket nodes).

1. **acme_billing_finance** - Invoices, payments, billing account, dunning
2. **acme_subscriptions_core** - Plans, seats, renewals
3. **acme_entitlements_state** - Workspace access, feature flags
4. **acme_support_tickets** - Customer complaints and support
5. **acme_product_contracts** - MSAs, order forms, pricing terms
6. **acme_audit_event_stream** - Temporal event log for timeline analysis
7. **Agent private datasets** - Private notes per agent type


**TICK-1001:** AcmeCorp workspace (W-332) was downgraded to `trial_read_only` even though invoice INV-94812 was **paid** and they have an active subscription. Customers were locked out.

**Root cause:** Billing account (BA-ACME-001) stayed `past_due` due to delayed webhook reconciliation; the entitlements service gated access on that stale status.

**Multi-agent investigation:** Support identifies complaints and impact; Billing verifies invoice and billing account; Entitlements traces the downgrade. The supervisor produces root cause and recommendation, stores it in the `agent_resolutions` dataset, then **TICK-1002** runs and that step reuses the stored TICK-1001 resolution.

## What happens when you run it

For each incident: Billing runs **two Cognee searches in the same session** (invoice, then billing account -- context preserved). Support and Entitlements run their searches. A final step gets **one TEMPORAL timeline** (audit events) and **similar past resolutions** from Cognee (empty for TICK-1001, TICK-1001's resolution for TICK-1002). After each incident, the summary and recommendation are stored in the **agent_resolutions** dataset so the next similar ticket benefits.

After both incidents, the **user isolation proof** runs: it shows that billing_user cannot search support_user's datasets (access denied), but supervisor_user can (shared read).

### Why Cognee? The contrast

**Without Cognee** you would have to:
- Query each system (billing DB, support tickets, audit log) separately — no shared graph
- Manually merge timelines from different sources — no single view
- Re-investigate every similar ticket — no memory of past resolutions
- Rely on ad-hoc access — no isolation between agents

**With Cognee** this demo gives you:
- **One graph** — billing, support, entitlements, and audit events in a single knowledge graph
- **One TEMPORAL search** — one query returns the timeline for an incident (no manual merge)
- **Memory** — TICK-1002 reuses TICK-1001’s resolution from the `agent_resolutions` dataset
- **User isolation** — each agent sees only its datasets; the supervisor has read access to all
- **Session context** — e.g. Billing’s two queries share the same session, so the second has context from the first

## Setup and run

From the **cognee repo root**, with venv active and `.env` containing `OPENAI_API_KEY` (or `LLM_API_KEY`):

```bash
# Install (if needed)
uv pip install -e ".[...]"   # from cognee-integration-langgraph dir, or as needed

# Load demo data into Cognee (prune, add per user, cognify, share permissions, sanity search)
python examples/data/cognee_mock_saas_entitlements_demo/load_into_cognee.py

# Run the multi-agent demo (TICK-1001 then TICK-1002, then user isolation proof)
python examples/saas_entitlements_agents.py
```

**Note:** Loading data may take a few minutes. Run `load_into_cognee.py` first or you'll get "data not found" style errors. The demo requires `ENABLE_BACKEND_ACCESS_CONTROL=True` and LanceDB + Kuzu (set via env vars or in the scripts).



## Troubleshooting

- **Data not found** - Run `load_into_cognee.py` first.
- **Cognee version** - Use a Cognee version compatible with this integration (see `pyproject.toml`).
- **User isolation requires LanceDB + Kuzu** - Set `ENABLE_BACKEND_ACCESS_CONTROL=True`, `VECTOR_DB_PROVIDER=lancedb`, `DB_PROVIDER=sqlite`. pgvector does not support access control.

## Memory reuse experiment (time comparison)

`examples/memory_reuse_experiment.py` runs the same supervisor pipeline under two conditions — **baseline** (graph search only) vs **built-in feedback** (Redis session cache + periodic memify to graph) — to measure how memory reuse affects investigation time. The experiment loads its own data; no separate `load_into_cognee.py` step is needed. Redis is required for the feedback condition (see Redis setup in the main [README](../README.md)).

**Example run** (2 incidents × 5 repeats = 10 runs per condition):

```
============================================================
  EXPERIMENT RESULTS  (time comparison)
============================================================
Incident        | Cause            |   Base | Feedback
------------------------------------------------------
TICK-1001 #1    | delayed_reconci  |   35s |   51s
TICK-1002 #1    | delayed_reconci  |   32s |   32s
TICK-1001 #2    | delayed_reconci  |   40s |   23s
TICK-1002 #2    | delayed_reconci  |   36s |   26s
TICK-1001 #3    | delayed_reconci  |   39s |   24s
TICK-1002 #3    | delayed_reconci  |   43s |   30s
TICK-1001 #4    | delayed_reconci  |   39s |   25s
TICK-1002 #4    | delayed_reconci  |   32s |   30s
TICK-1001 #5    | delayed_reconci  |   37s |   24s
TICK-1002 #5    | delayed_reconci  |   46s |   29s
------------------------------------------------------
TOTAL           |                  |  380s |  293s
============================================================
```

Feedback is ~23% faster overall (293s vs 380s); later runs reuse cached Q&A and memified knowledge.

**Session cache depth** (fill-and-flush cycle around each memify):

```
========================================================================
  SESSION CACHE DEPTH  (exp_builtin_analyst — Q&A pairs stored per run)
========================================================================
  TICK-1001 #1           │█████████ 6
  TICK-1002 #1           │███████████████████ 12
  TICK-1001 #2           │████████████████████████████ 18
  TICK-1002 #2           │██████████████████████████████████████ 24
                         └── [MEMIFY → persisted to graph]
  TICK-1001 #3           │█████████ 6
  TICK-1002 #3           │███████████████████ 12
  TICK-1001 #4           │████████████████████████████ 18
  TICK-1002 #4           │██████████████████████████████████████ 24
                         └── [MEMIFY → persisted to graph]
  TICK-1001 #5           │█████████ 6
  TICK-1002 #5           │███████████████████ 12
========================================================================
```

**How to run:** From the repo root (after Redis is set up for the feedback condition):

```bash
python examples/memory_reuse_experiment.py
```

Outputs: a per-incident timing table, the Redis cache depth chart above, `experiment_results.json`, and `experiment_graph.html` — an interactive view of all user graphs including the memified session nodes.

## Learn more

- [Cognee](https://github.com/topoteretes/cognee)
- [LangGraph](https://langchain-ai.github.io/langgraph/)
- [cognee-integration-langgraph](../README.md)
