# n8n-nodes-cognee

Use Cognee Cloud's AI memory and context engineering directly in your n8n workflows.

This community node lets you:

- **Remember** text or files, **Recall** with knowledge-graph search, and **Forget** data — Cognee's memory API in three operations
- Store session **Q&A, trace and feedback entries** so an AI Agent's conversation becomes searchable memory
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

## Operations

The node exposes six resources. Each operation maps to a Cognee `/api/v1` endpoint, the same API served by Cognee Cloud tenants and by a self-hosted cognee server (e.g. `http://localhost:8000`). Point the credential **Base URL** at whichever backend you use. The connection test hits `GET /health`.

### Resource: Memory

The memory-oriented API. **Remember** is the one-call path (add + cognify); **Recall** is a superset of Search that can also read session memory; **Forget** replaces the two Delete operations and adds memory-only clearing.

- **Operation: Remember** — `POST /api/v1/remember` (multipart/form-data)
  - Fields: Input Type (Text or Binary File), Text (multiple) or Input Binary Field, Dataset Name
  - Additional Fields: Dataset ID, Session ID, Node Set, Run in Background, Custom Prompt, Chunk Size, File Name Prefix
  - Text items are uploaded as `memory-N.txt` file parts; a binary input keeps its original file name and MIME type (PDF, DOCX, ...). Returns `status`, `dataset_id`, `pipeline_run_id` and per-file `items`.
- **Operation: Remember Entry** — `POST /api/v1/remember/entry`
  - Fields: Entry Type (Question and Answer / Trace / Feedback), Session ID, Dataset Name, plus the type's fields (Question + Answer; Origin Function + Status; QA ID)
  - Additional Fields: Context, Feedback Text, Feedback Score, Method Params, Method Return Value, Memory Query, Memory Context, Error Message, Dataset ID
  - Returns `entry_type` and `entry_id`; pass a Q&A's `entry_id` as QA ID to attach feedback later.
- **Operation: Recall** — `POST /api/v1/recall`
  - Fields: Query, Search Type (any Cognee search type, or **Auto** to let the server route the query), Datasets (empty = all you can read), Top K, Simplify
  - Additional Options: Session ID, Scope (graph / session / trace / session_context / all / tools / code), Only Context, Context Format, Context Profile, Dataset IDs, Node Sets, System Prompt, Include References, Verbose
  - With Simplify on (default) each hit becomes one item with `source` and `text` plus a few source-specific fields; the original entry is kept under `raw`.
- **Operation: Forget** — `POST /api/v1/forget`
  - Fields: Forget (Dataset / Data Item / Everything), Identify Dataset By (Name or ID), Dataset Name or Dataset ID, Data ID, Memory Only
  - Memory Only clears graph and vector data but keeps raw files, so the dataset can be re-cognified. **Everything** requires the explicit confirmation toggle and permanently deletes all datasets and data you own.

Example Recall body sent by the node:

```json
{
  "query": "Where was Einstein born?",
  "search_type": null,
  "datasets": ["facts"],
  "top_k": 5,
  "session_id": "chat-42",
  "scope": ["graph", "session"],
  "only_context": true
}
```

### Resource: Add Data

- **Operation**: Add
- **Endpoint**: `POST /api/v1/add` (multipart/form-data)
- **Fields**:
  - Dataset Name (`datasetName`, required): Name of the Cognee dataset to add text to (created if it does not exist)
  - Text Data (`textData`, required, multiple): Strings to store. Each item is uploaded as its own `text-N.txt` file part.
  - Additional Fields: Node Set (`node_set`, multiple) to tag the data for filtered search; Run in Background (`run_in_background`) to return immediately with a `pipeline_run_id`

The node builds the multipart body itself (no extra dependencies): one `data` file part per text item plus the `datasetName` and optional form fields.

### Resource: Cognify

- **Operation**: Cognify
- **Endpoint**: `POST /api/v1/cognify`
- **Fields**:
  - Datasets (`datasets`, required, multiple): One or more dataset names to cognify
  - Run in Background (`run_in_background`): Return immediately with a `pipeline_run_id`; poll `GET /api/v1/datasets/status` for completion
  - Additional Options: Dataset IDs (`dataset_ids`), Custom Prompt (`custom_prompt`), Chunk Size (`chunk_size`), Ontology Keys (`ontology_key`)

Example body sent by the node:

```json
{
  "datasets": ["support_docs"],
  "run_in_background": false
}
```

### Resource: Search

- **Operation**: Search
- **Endpoint**: `POST /api/v1/search`
- **Fields**:
  - Search Type (`search_type`): Any Cognee search type, e.g. `GRAPH_COMPLETION` (default), `HYBRID_COMPLETION`, `GRAPH_COMPLETION_COT`, `RAG_COMPLETION`, `CHUNKS`, `SUMMARIES`, `TEMPORAL`, `FEELING_LUCKY`, `CODE`, `AGENTIC_COMPLETION`
  - Datasets (`datasets`, required, multiple): Dataset names (resolve only to datasets you own)
  - Query (`query`, required)
  - Top K (`top_k`, optional number): Defaults to 10
  - Additional Options: Dataset IDs (`dataset_ids`, for shared datasets), System Prompt (`system_prompt`), Only Context (`only_context`), Node Sets (`node_name`), Session ID (`session_id`), Include References (`include_references`), Verbose (`verbose`)

Example body sent by the node:

```json
{
  "search_type": "GRAPH_COMPLETION",
  "datasets": ["support_docs"],
  "query": "How do I export my data?",
  "top_k": 5
}
```

### Resource: Delete

- **Operation**: Delete Dataset
- **Endpoint**: `DELETE /api/v1/datasets/{datasetId}`
- **Fields**:
  - Dataset ID (`datasetId`, required): The UUID of the dataset to delete

- **Operation**: Delete Data
- **Endpoint**: `DELETE /api/v1/datasets/{datasetId}/data/{dataId}`
- **Fields**:
  - Dataset ID (`datasetId`, required): The UUID of the dataset
  - Data ID (`dataId`, required): The UUID of the data item to remove

### Resource: Skill

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

Chat memory for an AI Agent:

1. **Recall** (Cognee) before the agent
   - Resource: Memory → Operation: Recall
   - Query: `{{ $json.chatInput }}`, Session ID (Additional Options): `{{ $json.sessionId }}`, Scope: Graph + Session, Only Context: on
   - Feed the returned `text` fields into the agent's system prompt as remembered context
2. **AI Agent** answers the user
3. **Remember Entry** (Cognee) after the agent
   - Resource: Memory → Operation: Remember Entry, Entry Type: Question and Answer
   - Session ID: `{{ $json.sessionId }}`, Question: the user message, Answer: the agent output
4. Optionally **Remember** documents the agent should know (Resource: Memory → Remember, Input Type: Binary File) from a Drive or email trigger.

End-to-end dataset workflow:

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

- **Unreleased**: Add the **Memory** resource: Remember (text or binary file, multipart), Remember Entry (qa / trace / feedback session entries), Recall (all search types plus Auto routing, session scope, Simplify output) and Forget (dataset, data item, memory-only, or everything behind a confirmation toggle). Move Add Data, Cognify, Search and Delete to the `/api/v1` endpoints (the legacy `/api/add_text`, `/api/cognify`, `/api/search` routes are no longer served). Add Data now uploads text as multipart file parts and gains Node Set / Run in Background. Search exposes all Cognee search types plus Dataset IDs, System Prompt, Only Context, Node Sets, Session ID, Include References and Verbose. Cognify gains Dataset IDs, Custom Prompt, Chunk Size and Ontology Keys. Icons now have light/dark variants; toolchain upgraded to `@n8n/node-cli` 0.46 with vitest unit tests.

- **0.5.0**: Add the **Skill** resource (self-improving skill loop) targeting the `/api/v1` API: Ingest Skill, Review Skill (agentic), Propose Improvement, Apply Improvement, Get Skill, Get Proposal. Existing Add/Cognify/Search/Delete operations are unchanged.

 - **0.4.0**: Prefix `/api` to all endpoint URLs and update Base URL format to `https://tenant-xxx.aws.cognee.ai` (breaking change — re-enter
  credential). Address n8n marketplace review

- **0.3.0**: Add request timeouts for all operations (5 min default, 10 min for Cognify). Enable `usableAsTool` for AI agent compatibility. Migrate tooling to `@n8n/node-cli`. Add GitHub Actions CI and publish workflows with npm provenance.
- **0.2.0**: Add Delete resource (Delete Dataset, Delete Data operations). Update API endpoints and base URL to Cognee Cloud.
- **0.1.0**: Initial release with Add Data, Cognify, and Search operations.

## License

MIT
