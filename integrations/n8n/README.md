# n8n-nodes-cognee

Use Cognee Cloud's AI memory and context engineering directly in your n8n workflows.

This community node lets you:

- Add text data to a Cognee dataset
- Turn data into AI memory with cognify to build knowledge-graph-based memory
- Run search over your AI memory datasets
- Recall from memory: a search alias with session, node-set filtering and auto-routing
- Remember text: one-shot add + cognify with session attribution and node-set tagging
- Delete datasets or individual data items
- Run the self-improving skill loop: ingest a SKILL.md, review a task with the skill loaded, propose an improvement, review the before/after diff, and apply it

[n8n](https://n8n.io/) is a fair-code licensed workflow automation platform.

## Table of contents

- [Installation](#installation)
- [Credentials](#credentials)
- [Operations](#operations)
- [Usage examples](#usage-examples)
- [Compatibility](#compatibility)
- [Resources](#resources)
- [Version history](#version-history)
- [License](#license)

## Installation

Install from within n8n:

1. In n8n, go to Settings → Community Nodes
2. Click Install and search for `n8n-nodes-cognee`, or paste the package name directly
3. Confirm the installation

Or install in your n8n instance directory:

```bash
npm install n8n-nodes-cognee
```

Restart n8n after installation if required.

## Credentials

Get your Cognee API key and Base URL from your [Cognee Cloud dashboard](https://docs.cognee.ai/how-to-guides/cognee-cloud) (API Keys page).

Create credentials of type `Cognee API` in n8n. The node uses these values to authenticate every request:

- **Base URL**: The base URL of your Cognee Cloud tenant, e.g. `https://tenant-xxx.aws.cognee.ai`. Do not include a trailing `/api` — the node appends it automatically.
- **API Key**: Your Cognee API key, sent via the `X-Api-Key` header.

## Operations

The node exposes seven resources. Each operation maps to a Cognee API endpoint.

> **Two API surfaces.** The **Add Data / Cognify / Search / Delete** resources call Cognee Cloud's `/api/*` endpoints. The **Recall**, **Remember** and **Skill** resources call the `/api/v1/*` endpoints (available on a self-hosted cognee server today, and on Cognee Cloud as its `/api/v1` surface rolls out). Point the credential **Base URL** at whichever backend exposes the routes you need (e.g. `http://localhost:8000` for a self-hosted server). The connection test hits `GET /health`.

### Resource: Add Data

- **Operation**: Add
- **Endpoint**: `POST /api/add_text`
- **Fields**:
  - Dataset Name (`datasetName`, required): Name of the Cognee dataset to add text to
  - Text Data (`textData`, required, multiple): Array of strings to store

Example body sent by the node:

```json
{
  "datasetName": "support_docs",
  "textData": [
    "FAQ: Reset password via account settings.",
    "Guide: Export data as CSV from dashboard."
  ]
}
```

### Resource: Cognify

- **Operation**: Cognify
- **Endpoint**: `POST /api/cognify`
- **Fields**:
  - Datasets (`datasets`, required, multiple): One or more dataset names to cognify

Example body sent by the node:

```json
{
  "datasets": ["support_docs"]
}
```

### Resource: Search

- **Operation**: Search
- **Endpoint**: `POST /api/search`
- **Fields**:
  - Search Type (`searchType`): One of `GRAPH_COMPLETION`, `GRAPH_COMPLETION_COT`, `RAG_COMPLETION`
  - Datasets (`datasets`, required, multiple)
  - Query (`query`, required)
  - Top K (`topK`, optional number): Defaults to 10

Example body sent by the node:

```json
{
  "searchType": "GRAPH_COMPLETION",
  "datasets": ["support_docs"],
  "query": "How do I export my data?",
  "topK": 5
}
```

### Resource: Recall (`/api/v1`)

Search wearing a memory hat. Same response as **Search**, plus session, node-set filtering, and an **Auto** search type that lets cognee route the query for you.

- **Operation**: Recall
- **Endpoint**: `POST /api/v1/recall`
- **Fields**:
  - Search Type (`search_type`): `Auto` (default; sends `null` for auto-routing), `GRAPH_COMPLETION`, `GRAPH_COMPLETION_COT`, or `RAG_COMPLETION`
  - Query (`query`, required)
  - Datasets (`datasets`, optional, multiple): omit to search every dataset you can read
  - Session ID (`session_id`, optional): recall from a session's cached QA/trace entries
  - Node Names (`node_name`, optional, multiple): restrict to node sets tagged via Remember/Add
  - Scope (`scope`): `auto`, `graph`, `session`, `trace`, `graph_context`, or `all`
  - Top K (`top_k`, optional number): defaults to 15

**vs Search**: Recall hits `/api/v1/recall` (search's memory alias) and adds `session_id`, `node_name` and `scope`, plus an Auto type that sends `search_type: null`. The response shape matches Search (raw body passthrough).

Example body sent by the node:

```json
{
  "search_type": null,
  "query": "How do I export my data?",
  "datasets": ["support_docs"],
  "session_id": "claude-code-1718000000",
  "node_name": ["support"],
  "scope": "auto",
  "top_k": 15
}
```

### Resource: Remember (`/api/v1`)

One-shot add + cognify: ingest text and build the knowledge graph in a single call, with session attribution and node-set tagging.

- **Operation**: Remember
- **Endpoint**: `POST /api/v1/remember` (multipart form)
- **Fields**:
  - Dataset Name (`datasetName`, required): created if it does not exist
  - Dataset ID (`datasetId`, optional): UUID of an existing dataset, used instead of resolving by name
  - Text (`rememberText`, required): sent as an uploaded `.txt` file part
  - Session ID (`session_id`, optional): attribute the memory to a session (tracked in the sessions dashboard)
  - Node Sets (`node_set`, optional, multiple): tag the data so Recall/Search can later filter to it
  - Run in Background (`run_in_background`, default on): the request returns as soon as the work is enqueued. Disable to wait synchronously; note the Cognee Cloud gateway closes long-running connections around the 4-minute mark, so non-trivial texts fail with ECONNRESET in sync mode.

**vs Add + Cognify**: Remember does add and cognify in one request. What it buys you over two nodes is `session_id` attribution and `node_set` tagging. If you only need plain-text ingest without those, **Add Data** + **Cognify** does the same job.

### Resource: Delete

- **Operation**: Delete Dataset
- **Endpoint**: `DELETE /api/datasets/{datasetId}`
- **Fields**:
  - Dataset ID (`datasetId`, required): The UUID of the dataset to delete

- **Operation**: Delete Data
- **Endpoint**: `DELETE /api/datasets/{datasetId}/data/{dataId}`
- **Fields**:
  - Dataset ID (`datasetId`, required): The UUID of the dataset
  - Data ID (`dataId`, required): The UUID of the data item to remove

### Resource: Skill (`/api/v1`)

The self-improving skill loop. A weak run becomes a reviewable, approvable edit to a skill's instructions.

- **Operation: Ingest Skill** — `POST /api/v1/skills`
  - Fields: Skill Name, Dataset Name, Skill Markdown (inline SKILL.md body)
  - Ingests the markdown as a dataset-scoped Skill node (no file upload needed). Returns the dataset id.
- **Operation: Review Skill** — `POST /api/v1/search` (`search_type=AGENTIC_COMPLETION`)
  - Fields: Skill Name, Dataset Name, Query, Max Iterations, Top K
  - Runs an agentic completion with the skill loaded, so you can grade how well the skill handled the task.
- **Operation: Propose Improvement** — `POST /api/v1/remember/entry`
  - Fields: Skill Name, Dataset Name, Task Text, Result Summary, Success Score, Score Threshold
  - Records the weak run and creates a `SkillImprovementProposal` (status `proposed`, **not** applied). Returns `proposal_id`.
- **Operation: Get Proposal** — `GET /api/v1/proposals/{proposalId}`
  - Fields: Proposal ID, Dataset ID
  - Returns `old_procedure`, `proposed_procedure`, `rationale`, `confidence` — review the diff **before** approving.
- **Operation: Apply Improvement** — `POST /api/v1/remember/entry` (`skill_improvement.apply=true`)
  - Fields: Skill Name, Dataset Name, Proposal ID
  - Applies the approved proposal, writing the new procedure into the skill.
- **Operation: Get Skill** — `GET /api/v1/skills/{skillId}`
  - Fields: Skill ID, Dataset ID
  - Returns one skill including its full `procedure` body (useful to confirm the applied change).

Loop wiring: **Ingest Skill** → **Review Skill** → (score in n8n) → **Propose Improvement** → **Get Proposal** (show diff for approval) → **Apply Improvement** → **Get Skill**.

## Usage examples

End-to-end example workflow:

1. **Add Data** (Cognee)
   - Resource: Add Data → Operation: Add
   - Dataset Name: `support_docs`
   - Text Data: Add one or more strings with your content
2. **Cognify** (Cognee)
   - Resource: Cognify → Operation: Cognify
   - Datasets: `support_docs`
3. **Search** (Cognee)
   - Resource: Search → Operation: Search
   - Search Type: `GRAPH_COMPLETION`
   - Datasets: `support_docs`
   - Query: Your question, e.g. "How do I export my data?"
   - Top K: `5`
4. **Delete** (Cognee)
   - Resource: Delete → Operation: Delete Dataset
   - Dataset ID: UUID of the dataset to remove

Troubleshooting:

- 401/403 errors: Check the API key and that `X-Api-Key` is accepted by your Cognee instance.
- Connection errors: Verify Base URL and network access from your n8n host.

## Compatibility

- Node.js: >= 20.15
- n8n Nodes API: v1

The node depends on `n8n-workflow` at runtime (peer dependency). It should work on current n8n releases supporting community nodes.

## Resources

- [Cognee Cloud docs](https://docs.cognee.ai/how-to-guides/cognee-cloud)
- [Package homepage](https://github.com/topoteretes/cognee-n8n)

## Version history

- **0.6.0**: Add the **Recall** and **Remember** resources targeting the `/api/v1` API. Recall (`POST /api/v1/recall`) is a memory-oriented search alias with `session_id`, `node_name`, `scope` and an Auto (`search_type: null`) routing option. Remember (`POST /api/v1/remember`) does one-shot add + cognify of text with session attribution and node-set tagging. Existing operations are unchanged.
- **0.5.1**: Review Skill now returns a parsed score item (`score`, `score_parse_ok`, `dimensions`, `review`) instead of the raw search body, and the credential connection test hits `GET /health` instead of `GET /api/health`.
- **0.5.0**: Add the **Skill** resource (self-improving skill loop) targeting the `/api/v1` API: Ingest Skill, Review Skill (agentic), Propose Improvement, Apply Improvement, Get Skill, Get Proposal. Existing Add/Cognify/Search/Delete operations are unchanged.

 - **0.4.0**: Prefix `/api` to all endpoint URLs and update Base URL format to `https://tenant-xxx.aws.cognee.ai` (breaking change — re-enter
  credential). Address n8n marketplace review

- **0.3.0**: Add request timeouts for all operations (5 min default, 10 min for Cognify). Enable `usableAsTool` for AI agent compatibility. Migrate tooling to `@n8n/node-cli`. Add GitHub Actions CI and publish workflows with npm provenance.
- **0.2.0**: Add Delete resource (Delete Dataset, Delete Data operations). Update API endpoints and base URL to Cognee Cloud.
- **0.1.0**: Initial release with Add Data, Cognify, and Search operations.

## License

MIT
