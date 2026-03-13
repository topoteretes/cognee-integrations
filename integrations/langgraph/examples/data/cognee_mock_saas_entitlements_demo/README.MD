# Cognee Mock Datasets — Subscription & Entitlements Copilot Demo

**For the full demo description and how to run the agents, see [examples/README_SAAS_DEMO.md](../examples/README_SAAS_DEMO.md).**

This bundle contains mock datasets aligned to the demo:
- NodeSets (tags on ingestion)
- Finance + subscription + entitlements drift
- Temporal event stream for Temporal Cognify
- Multi-tenant (tenant_acme + tenant_beta)
- Agent-private datasets (to demonstrate isolation)

## Folder layout
- `tenant_acme/` — the main demo tenant
- `tenant_beta/` — minimal second tenant for isolation demos
- `ontologies/saas_finance_subset.ttl` — tiny demo ontology subset

## Key incident IDs (tenant_acme)
- Invoice: `INV-94812` (paid at 2026-01-03T08:17:12Z)
- Billing account: `BA-ACME-001` (incorrectly stuck in `past_due` at 08:20Z)
- Subscription: `SUB-483` (seats purchased -> 50)
- Workspace: `W-332` (entitlements fallback applied at 08:22Z)

## Loading into Cognee
Run `load_into_cognee.py` (from cognee repo root). It prunes, adds all datasets with their node sets, runs cognify (including temporal for the audit stream), and runs a sanity search.

Notes:
- Cognee `.add()` supports raw text, files, or directories (walked recursively).
- NodeSets are passed via `node_set=[...]` on `add()`.
- Temporal mode: `await cognee.cognify(datasets=[...], temporal_cognify=True)`.
- Memory: incident resolutions are stored via `cognee.add()` into the `agent_resolutions` dataset and retrieved via graph search.