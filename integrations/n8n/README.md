# n8n-nodes-cognee

Use Cognee Cloud's AI memory and context engineering directly in your n8n workflows.

This community node lets you:

- Add text data to a Cognee dataset
- Turn data into AI memory with cognify to build knowledge-graph-based memory
- Run search over your AI memory datasets
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

## Runtime modes

Cognee integrations use the same runtime model:

| Mode | When to use it | How it talks to Cognee |
| --- | --- | --- |
| **local-server** (default) | You want local data with safe concurrent access | Starts or connects to a local Cognee server, then uses HTTP as a thin client |
| **cloud** | `COGNEE_BASE_URL` points to a managed or remote Cognee service | Uses HTTP as a thin client with `COGNEE_API_KEY` |
| **embedded** | You explicitly choose in-process Cognee for a single process or offline run | Runs Cognee inside the integration process |

**Why local-server is the safe default.** Cognee local stores, including SQLite, Kuzu, Ladybug, and LanceDB, are single-writer stores. If hooks, multiple terminals, or another integration use the same data root in embedded mode, they can hit `database is locked` errors or corrupt local state. A local Cognee server avoids that by owning the stores and serializing access. Each integration talks to it over HTTP.

**No silent fallbacks.** A configured cloud endpoint should fail clearly if it is unreachable. A local server should fail clearly if it cannot start. Falling back to another mode can hide configuration errors or write data to the wrong store. Use embedded mode only when you accept the single-process tradeoff.

## Operations

The node exposes five resources. Each operation maps to a Cognee API endpoint.

> **Two API surfaces.** The **Add Data / Cognify / Search / Delete** resources call Cognee Cloud's `/api/*` endpoints. The **Skill** resource (self-improving loop) calls the `/api/v1/*` endpoints — available on a self-hosted cognee server today, and on Cognee Cloud as its `/api/v1` surface rolls out. Point the credential **Base URL** at whichever backend exposes the routes you need (e.g. `http://localhost:8000` for a self-hosted server). The connection test hits `GET /health`.

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

- **0.5.0**: Add the **Skill** resource (self-improving skill loop) targeting the `/api/v1` API: Ingest Skill, Review Skill (agentic), Propose Improvement, Apply Improvement, Get Skill, Get Proposal. Existing Add/Cognify/Search/Delete operations are unchanged.

 - **0.4.0**: Prefix `/api` to all endpoint URLs and update Base URL format to `https://tenant-xxx.aws.cognee.ai` (breaking change — re-enter
  credential). Address n8n marketplace review

- **0.3.0**: Add request timeouts for all operations (5 min default, 10 min for Cognify). Enable `usableAsTool` for AI agent compatibility. Migrate tooling to `@n8n/node-cli`. Add GitHub Actions CI and publish workflows with npm provenance.
- **0.2.0**: Add Delete resource (Delete Dataset, Delete Data operations). Update API endpoints and base URL to Cognee Cloud.
- **0.1.0**: Initial release with Add Data, Cognify, and Search operations.

## License

MIT
