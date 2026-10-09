# Cognee nightly memory promotion for n8n

A scheduled workflow that promotes every Cognee session active in the last 24
hours into the knowledge graph, once a day, with one request. It is the
fixed-hour companion to the **Cognee Memory** sub-node's own promotion, and the
only way to promote sessions that were written by something other than n8n.

## What it does

1. **Every 24 Hours** *(Schedule Trigger)*
2. **Sessions Active Today** *(Cognee → Session → Get Many)* — sessions whose
   last activity falls in the last 24 hours, newest first (up to 500).
3. **Collect Session IDs** *(Aggregate)* — one item carrying all `session_id`
   values as `session_ids`.
4. **Any Sessions?** *(IF)* — skips the day when nothing happened.
5. **Promote Sessions Into Graph** *(Cognee → Memory → Improve)* — one
   `POST /api/v1/improve` with every session ID. The server reads its own
   session cache, so no session text travels in the request, and improve is
   idempotent per session: a run only processes the turns added since the
   previous one.

## Prerequisites

- A Cognee server (self-hosted with `CACHING=true`, or Cognee Cloud) and an API
  key.
- The **Cognee** community node (`n8n-nodes-cognee` ≥ 0.8.0) installed in n8n,
  which adds the **Memory → Improve** operation.

## Setup

1. Import `workflow.json`.
2. Select your **Cognee API** credential on both Cognee nodes.
3. Set **Dataset Name** on *Promote Sessions Into Graph* to the dataset your
   Cognee Memory sub-nodes write to (their *Dataset Name* option, `main_dataset`
   by default). For a dataset shared with you, use *Additional Fields → Dataset
   ID* instead.
4. Activate the workflow.

## How it relates to the sub-node's own promotion

The **Cognee Memory** sub-node promotes on its own, by default at most once
every 24 hours at the end of the first execution after the interval has passed.
That is traffic-driven: a conversation that goes quiet after the daily run waits
for the next message to arrive before it is promoted. This workflow closes that
gap at a fixed hour. Keep both, or set the sub-node's **Promote To Graph** to
*Never* and let this workflow do all promotion.
