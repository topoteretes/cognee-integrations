# Cognee Skill Self-Improvement — no-code / Verified Node build

A fully node-native version of the self-improving skill loop. Every Cognee step
is a **Cognee Verified Node** operation (no Python, no Execute Command, no shell
scripts). n8n owns scoring, the threshold gate, the approval gate, and the diff.

This build talks to Cognee's `/api/v1` API (the **Skill** resource on the node),
so it runs against a self-hosted Cognee server today, and against Cognee Cloud as
its `/api/v1` surface rolls out.

## What it does

1. **Demo Controls** — sets the skill name, dataset, the SKILL.md markdown, the
   review task, the evaluator score, the threshold, and the approval flag.
2. **Ingest Skill** *(Cognee)* — ingests the inline SKILL.md into the dataset.
3. **Review Skill** *(Cognee)* — runs an `AGENTIC_COMPLETION` search with the skill
   loaded against the review task.
4. **Should Improve?** *(IF)* — `eval_score < score_threshold`.
5. **Propose Improvement** *(Cognee)* — records the weak run and creates a
   proposal (not applied). Returns `proposal_id`.
6. **Get Proposal** *(Cognee)* — fetches `old_procedure` / `proposed_procedure` /
   `rationale` / `confidence`.
7. **Build Review Packet** *(Code)* — computes the before/after diff so you can
   review it **before** approving.
8. **Approved?** *(IF)* — gates on the approval flag.
9. **Apply Improvement** *(Cognee)* — applies the approved proposal.
10. **Get Skill** + **Show Skill Delta** — confirms the updated procedure and emits
    `skill_delta_markdown`.

## Prerequisites

- A running Cognee server that exposes `/api/v1` (e.g. a local OSS checkout:
  `cognee serve` / uvicorn on `http://localhost:8000`), with an LLM + embedding
  provider configured (the review and proposal steps call the LLM).
- An API key for that server.
- The **Cognee** community node (`n8n-nodes-cognee` ≥ 0.5.0) installed in n8n.

## Setup

1. In n8n: **Settings → Community Nodes → Install** `n8n-nodes-cognee`.
2. Create **Cognee API** credentials: Base URL = your server (e.g.
   `http://localhost:8000`), API Key = your key. (On a self-hosted server the
   credential connection test hits `/api/health` and may 404 — that's fine, the
   operations still work.)
3. Import `workflow.json` and select your Cognee credential on the Cognee nodes.
4. Run the workflow.

## Approval gate

The demo approves via the `approved` flag in **Demo Controls**. For production,
replace the **Approved?** branch with a **Slack** node that posts
`skill_delta_markdown` followed by a **Wait for webhook** approval, and gate
**Apply Improvement** on the response (see the sticky note in the canvas).

## Why a few values are demo-set

`eval_score` is set in **Demo Controls** (n8n owns scoring) rather than read back
from the agent — keeping the threshold decision in n8n, not letting the agent
grade its own work. Swap in your own evaluator (an LLM-as-judge node, a CI score,
etc.) and wire its output into the **Should Improve?** comparison.
