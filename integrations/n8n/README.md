# n8n-nodes-cognee

Use Cognee Cloud's AI memory and context engineering directly in your n8n workflows.

The package ships two nodes:

- **Cognee** — an action node covering the Cognee `/api/v1` API (memory, datasets, search, skills)
- **Cognee Memory** — an AI Agent **memory sub-node**: plug it into the Agent's Memory port and the agent remembers across conversations. Turns are stored in a Cognee session, relevant knowledge is recalled for every question, and sessions are promoted into the knowledge graph once a day

This community node package lets you:

- Give an **AI Agent long-term memory** with the Cognee Memory sub-node — what a user said last week is recalled in a new conversation, with no extra nodes and no separate Redis or Postgres
- **Remember** text or files, **Recall** with knowledge-graph search, and **Forget** data — Cognee's memory API in three operations
- Store session **Q&A, trace and feedback entries** and search them back with session-scoped Recall
- Add text data to a Cognee dataset
- Turn data into AI memory with cognify, and enrich an existing graph with memify
- Run search over your AI memory datasets
- Manage datasets: create, list, inspect data items, poll processing status
- Inspect sessions and their usage
- Delete datasets or individual data items
- Run the self-improving skill loop: ingest a SKILL.md, review a task with the skill loaded, propose an improvement, review the before/after diff, and apply it

[n8n](https://n8n.io/) is a fair-code licensed workflow automation platform.

## Table of contents

- [Installation](#installation)
- [Credentials](#credentials)
- [Sub-node: Cognee Memory](#sub-node-cognee-memory)
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

## Sub-node: Cognee Memory

> **Self-hosted prerequisite**: the session cache must be enabled on your Cognee server (`CACHING=true`). Without it `POST /api/v1/remember/entry` answers **503** and the sub-node cannot store turns. Cognee Cloud has it enabled already.

`Cognee Memory` is a memory sub-node for n8n's **AI Agent** node, built on [`@n8n/ai-node-sdk`](https://github.com/n8n-io/n8n/tree/master/packages/%40n8n/ai-node-sdk). It appears next to Simple Memory, Redis Chat Memory and Postgres Chat Memory when you click the Agent's **Memory** port.

**Use it when** you want an agent that remembers across conversations. A stock memory sub-node replays one session's transcript. Cognee Memory does that too, and on top of it does what Cognee's coding-agent plugins do: it recalls relevant knowledge from the graph for every question, and it promotes the sessions it writes into the knowledge graph, so a user who comes back tomorrow under a new session ID is remembered.

What it does per agent turn:

- **Load**: `GET /api/v1/sessions/{sessionId}` — the last *Window Size* question/answer pairs of the session are handed to the agent as chat history
- **Recall**: `POST /api/v1/recall` — a hybrid completion search for the incoming question with `only_context: true` and `context_format: "context"`; the retrieved context is appended to the history as one message, right before the question. Servers older than the context format option return the full prompt envelope instead, which the node trims to the retrieved context the same way the Cognee coding-agent plugins do.
- **Save**: `POST /api/v1/remember/entry` — the user message and the agent's reply are stored as one `qa` session entry
- **Promote** (end of execution, at most once per interval): `POST /api/v1/improve` with every session written since the last run — the server bridges those sessions into the dataset's knowledge graph

The turns land in a Cognee session, so the same conversation is reachable from the **Cognee** node (Memory → Recall with the Session ID, Session → Get) and from any other Cognee client. Recalled context is handed to the agent only; it is never written into the session, so n8n's **Chat Memory Manager** still sees the real transcript.

Parameters:

- **Session ID** (required, default `{{ $json.sessionId }}`): the Cognee session to store the conversation under. Use the chat trigger's session ID or any stable per-user/per-conversation key.
- **Options → Dataset Name** (default `main_dataset`): the dataset the session is *attributed* to and promoted into, and the dataset recall reads from unless Recall Datasets is set. It does not scope the history: sessions are keyed per Cognee user and session ID, not per dataset, and a later write does not move an existing session. Reusing one Session ID under two dataset names mixes a single history rather than splitting it, so keep Session IDs globally unique or namespace them yourself.
- **Options → Dataset ID**: attribute by dataset UUID instead of by name. Required for a dataset shared with you, because a name only resolves among datasets you own. Takes precedence over Dataset Name.
- **Options → Window Size** (default 5, max 20): number of recent Q&A pairs loaded into the agent context. Cognee's session detail endpoint returns the most recent 20 pairs. Older turns are reachable through recall once promoted.
- **Options → Recall Context** (default on): recall knowledge for every question. **Recall Query** (default `{{ $json.chatInput }}`) is resolved from the item entering the agent, so it works with the Chat Trigger and with messaging triggers that put the message on the item; when it resolves to nothing, recall is skipped for that turn. **Recall Scope** (default Graph): the session scopes read the session the window already carries, so adding them mostly duplicates it. **Recall Datasets** (comma-separated, default the memory dataset), **Recall Top K** (default 5), **Max Context Characters** (default 12000, cut at a line break).
- **Options → Inject Context As** (default System Message): the role of the message carrying the recalled context. Switch to User Message for chat models that reject a system message that is not the first message.
- **Options → Promote To Graph** (default Every N Hours, with **Promote Every (Hours)** = 24): when sessions are promoted. *Every N Hours* promotes every session written since the last run at the end of the first execution after the interval has passed, in one request. *After Each Execution* promotes this session at the end of every execution, for immediate cross-conversation memory at the cost of one improve run per message. *Never* keeps turns in the session only; promote them with a separate workflow (Cognee → Memory → Improve) such as the shipped [nightly promotion workflow](../../n8n_workflows/cognee_nightly_memory_promotion).

Notes:

- The time of the last promotion and the sessions written since, grouped by the dataset they go into, are kept in the workflow's static data, which n8n only saves for **production** executions. Manual test runs from the editor promote every time, which is what you want when trying it out. n8n saves static data by overwriting it, so two executions finishing at the same instant can lose each other's queued session; the nightly workflow is the backstop for that.
- Promotion sends one improve request per dataset, so a workflow whose Dataset Name is an expression (one dataset per customer, say) promotes each session into its own dataset. The last-run time only advances once every dataset was accepted. A busy server, one whose improve lock is held by another run, leaves the sessions queued and the next execution retries; a failed request does the same and logs a warning. If the queue reaches 1000 sessions, promotion runs at the next execution regardless of the interval, so nothing is dropped under normal load.
- Promotion is traffic-driven: a conversation that goes quiet after the daily run is promoted when the next execution past the interval ends. The nightly workflow closes that gap at a fixed hour.
- Recall and promotion never fail the agent's turn. A failed recall logs a warning and the agent gets its history; a failed promotion logs a warning and is retried on the next execution.
- Improve is idempotent per session: every stage is guarded by a per-session watermark, so a run only processes the turns added since the previous one, and cost scales with what was said rather than with session length.
- Works with n8n's **Chat Memory Manager** node for **Get Many Messages** and **Insert Messages**. A user message immediately followed by an assistant message is stored as one Q&A entry. Any other message is stored half-filled, with only the side it belongs to, so inserting messages one at a time reads back as exactly those messages. System and tool messages keep their role in the entry's context field. A message with no text is skipped, since there is nothing to store.
- **Clearing a session is not supported yet**, because Cognee has no endpoint that deletes a single session. The Chat Memory Manager operations that wipe memory first — **Delete Messages**, and **Insert Messages** with **Override All Messages** — fail with an explanatory error rather than silently doing nothing. Start a new Session ID for a fresh conversation, or use Cognee → Memory → Forget on the dataset.
- For retrieval the agent should *decide* to run, attach the **Cognee** node to the agent's **Tool** port as well. The memory port's recall is ambient context the agent always gets; a tool is a search the agent runs when it judges it needs one.
- Requires n8n **2.16 or newer** (the release that made `@n8n/ai-node-sdk` available to community nodes). The Cognee action node itself has no such requirement.

## Operations

The Cognee action node exposes eight resources. Each operation maps to a Cognee `/api/v1` endpoint, the same API served by Cognee Cloud tenants and by a self-hosted cognee server (e.g. `http://localhost:8000`). Point the credential **Base URL** at whichever backend you use. The connection test hits `GET /health`.

### Resource: Memory

The memory-oriented API. **Remember** is the one-call path (add + cognify); **Recall** is a superset of Search that can also read session memory; **Forget** replaces the two Delete operations and adds memory-only clearing.

- **Operation: Remember** — `POST /api/v1/remember` (multipart/form-data)
  - Fields: Input Type (Text or Binary File), Text (multiple) or Input Binary Field, Dataset Name
  - Additional Fields: Dataset ID, Session ID, Node Set, Run in Background, Custom Prompt, Chunk Size, Chunks Per Batch, Ontology Keys, Graph Model (JSON schema with a top-level `title`), File Name Prefix
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
- **Operation: Improve** — `POST /api/v1/improve`
  - Fields: Session IDs (comma-separated, or an expression resolving to an array), Dataset Name
  - Additional Fields: Dataset ID, Run in Background (default on)
  - Promotes the session memory of those sessions into the dataset's knowledge graph, so later recalls find what was said. The server reads its own session cache, so no session text travels in the request. Idempotent per session: a run only processes entries added since the previous one. This is what the Cognee Memory sub-node calls on its own schedule; use it directly from a scheduled workflow, see the shipped [nightly promotion workflow](../../n8n_workflows/cognee_nightly_memory_promotion).

- **Operation: Update** — `PATCH /api/v1/update` (multipart/form-data)
  - Fields: Dataset Name or ID (dropdown), Data ID, Input Type (Text or Binary File), Text or Input Binary Field
  - Update Fields: File Name, Node Set
  - Replaces one data item: the old version is deleted and the new content is ingested into the graph.

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

- **Operation**: Memify
- **Endpoint**: `POST /api/v1/memify`
- **Fields**: Dataset Name, Run in Background; Additional Options: Dataset Name or ID, Extraction Tasks, Enrichment Tasks, Data, Node Sets
- Runs Cognee enrichment tasks over an existing dataset graph, or over custom Data with custom extraction/enrichment tasks.

- **Operation**: Cognify
- **Endpoint**: `POST /api/v1/cognify`
- **Fields**:
  - Datasets (`datasets`, required, multiple): One or more dataset names to cognify
  - Run in Background (`run_in_background`): Return immediately with a `pipeline_run_id`; poll `GET /api/v1/datasets/status` for completion
  - Additional Options: Dataset IDs (`dataset_ids`), Custom Prompt (`custom_prompt`), Chunk Size (`chunk_size`), Chunks Per Batch (`chunks_per_batch`), Data Per Batch (`data_per_batch`), Ontology Keys (`ontology_key`), Graph Model (`graph_model`, JSON schema with a top-level `title`)

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
  - Additional Options: Dataset IDs (`dataset_ids`, for shared datasets), System Prompt (`system_prompt`), Only Context (`only_context`), Context Format (`context_format`), Node Sets (`node_name`), Session ID (`session_id`), Include References (`include_references`), Verbose (`verbose`)

Example body sent by the node:

```json
{
  "search_type": "GRAPH_COMPLETION",
  "datasets": ["support_docs"],
  "query": "How do I export my data?",
  "top_k": 5
}
```

### Resource: Dataset

- **Operation: Get Many** — `GET /api/v1/datasets`: one item per dataset (`id`, `name`, `created_at`, `owner_id`)
- **Operation: Create** — `POST /api/v1/datasets`: Name (returns the existing dataset if the name is taken)
- **Operation: Get Data Items** — `GET /api/v1/datasets/{datasetId}/data`: Dataset Name or ID (dropdown); one item per stored document with its UUID
- **Operation: Get Status** — `GET /api/v1/datasets/status`: Dataset Names or IDs (empty = all), Pipelines (add / cognify / code graph). Returns one item per dataset (and per pipeline when several are chosen) with `dataset_id` and `status`. Poll this after **Run in Background**.
- **Operation: Get Progress** — `GET /api/v1/datasets/status/progress`: same selection, each item also carries `progress` (files completed / total, current stage)

Every "Dataset Name or ID" field is a dropdown loaded from your datasets; an expression can still supply a UUID.

### Resource: Session

- **Operation: Get Many** — `GET /api/v1/sessions`: Time Range (24h / 7d / 30d / all), Limit; Options: Status, Order By, Descending, Offset. One item per session.
- **Operation: Get** — `GET /api/v1/sessions/{sessionId}`: full session detail including Q&A and trace entries, usage and cost

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
  - Fields: Skill ID, Dataset Name or ID
  - Returns one skill including its full `procedure` body (useful to confirm the applied change).
- **Operation: Get Many** — `GET /api/v1/skills/`
  - Fields: Dataset Name or ID; Options: Include Inactive, Limit, Offset
  - Lists the skills ingested into a dataset.
- **Operation: Delete Skill** — `DELETE /api/v1/skills/{skillId}`
  - Fields: Skill ID, Dataset Name or ID
  - Removes the skill node and its embeddings from the dataset.

Loop wiring: **Ingest Skill** → **Review Skill** → (score in n8n) → **Propose Improvement** → **Get Proposal** (show diff for approval) → **Apply Improvement** → **Get Skill**.

## Usage examples

Long-term memory for an AI Agent (recommended):

1. **Chat Trigger** → **AI Agent**
2. Click the Agent's **Memory** port and pick **Cognee Memory**
   - Credential: your `Cognee API` credential
   - Session ID: `{{ $json.sessionId }}` (the chat trigger's session)
   - Options → Dataset Name: `support_chat`
3. Chat: "I play tennis on Tuesdays." The turn is stored in the session and, at the end of the execution, promoted into `support_chat`.
4. Open a **new** chat (a new session ID) and ask what sport you play. The recall step finds the promoted turn in the graph and the agent answers from it, with no Cognee tool attached.
5. Optionally add the **Cognee** node as an Agent **Tool** (Memory → Recall over the datasets you have ingested) for searches the agent decides to run itself, and import the [nightly promotion workflow](../../n8n_workflows/cognee_nightly_memory_promotion) to promote at a fixed hour.

Manual chat memory with the Cognee action node (when you need full control over what is recalled):

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
- `connect ECONNREFUSED ::1:8000` against a local server: n8n runs on Node 18+, which resolves `localhost` to the IPv6 address `::1`, while a self-hosted cognee server usually listens on IPv4 only. Use `http://127.0.0.1:8000` as the Base URL instead of `http://localhost:8000`.

## Compatibility

- Node.js at runtime: >= 20.15 (whatever your n8n instance runs on; the published package has no native dependencies)
- Node.js for development: >= 24 (see `.nvmrc`). The dev toolchain pulls in `isolated-vm`, a native module that compiles at install time and needs Node 24 or newer, so `npm install` fails on Node 20.
- n8n Nodes API: v1; AI Node SDK version: 1
- The **Cognee Memory** sub-node needs n8n ≥ 2.16 (April 2026), the first release that exposes `@n8n/ai-node-sdk` to community nodes. The **Cognee** action node works on any n8n release that supports community nodes.

The package depends on `n8n-workflow` and `@n8n/ai-node-sdk` at runtime (peer dependencies).

## Resources

- [Cognee Cloud docs](https://docs.cognee.ai/how-to-guides/cognee-cloud)
- [Package homepage](https://github.com/topoteretes/cognee-n8n)

## Version history

- **0.8.0**: The **Cognee Memory** sub-node becomes long-term memory. It recalls graph context for every question (`POST /api/v1/recall`, hybrid completion, only context) and appends it to the loaded history, and promotes the sessions it writes into the knowledge graph (`POST /api/v1/improve`) at most once per interval (default 24 hours), covering every session written since the last run in one request; the last-run time lives in the workflow static data. Both are on by default and switchable in the options. Window Size default drops to 5. The action node gains **Memory → Improve**, and the repo ships a nightly promotion workflow.
- **0.7.1**: Text uploads (Add Data, Remember) are named by a hash of their content instead of their position, so cognee >= 1.6.0 no longer refuses every text after the first run as a changed document.
- **0.7.0**: Add the **Cognee Memory** sub-node for the AI Agent's Memory port, built on `@n8n/ai-node-sdk`: loads the last N Q&A pairs of a Cognee session (`GET /api/v1/sessions/{id}`) and stores each turn as a `qa` session entry (`POST /api/v1/remember/entry`), so the conversation survives restarts and is readable from any Cognee client. Session entries stay in the session cache, separate from a dataset's knowledge graph. Declares `n8n.aiNodeSdkVersion: 1`; the sub-node requires n8n ≥ 2.16.
- **0.6.0**: Add the **Dataset** resource (Get Many, Create, Get Data Items, Get Status, Get Progress) and **Session** resource (Get Many, Get); Memify under Cognify; Update under Memory; Get Many and Delete Skill under Skill. Dataset ID fields become dropdowns loaded from your datasets. Add the **Memory** resource: Remember (text or binary file, multipart), Remember Entry (qa / trace / feedback session entries), Recall (all search types plus Auto routing, session scope, Simplify output) and Forget (dataset, data item, memory-only, or everything behind a confirmation toggle). Move Add Data, Cognify, Search and Delete to the `/api/v1` endpoints (the legacy `/api/add_text`, `/api/cognify`, `/api/search` routes are no longer served). Add Data now uploads text as multipart file parts and gains Node Set / Run in Background. Search exposes all Cognee search types plus Dataset IDs, System Prompt, Only Context, Node Sets, Session ID, Include References and Verbose. Cognify gains Dataset IDs, Custom Prompt, Chunk Size and Ontology Keys. Icons now have light/dark variants; toolchain upgraded to `@n8n/node-cli` 0.46 with vitest unit tests. Recall and Remember close topoteretes/cognee#3560.

- **0.5.0**: Add the **Skill** resource (self-improving skill loop) targeting the `/api/v1` API: Ingest Skill, Review Skill (agentic), Propose Improvement, Apply Improvement, Get Skill, Get Proposal. Existing Add/Cognify/Search/Delete operations are unchanged.

 - **0.4.0**: Prefix `/api` to all endpoint URLs and update Base URL format to `https://tenant-xxx.aws.cognee.ai` (breaking change — re-enter
  credential). Address n8n marketplace review

- **0.3.0**: Add request timeouts for all operations (5 min default, 10 min for Cognify). Enable `usableAsTool` for AI agent compatibility. Migrate tooling to `@n8n/node-cli`. Add GitHub Actions CI and publish workflows with npm provenance.
- **0.2.0**: Add Delete resource (Delete Dataset, Delete Data operations). Update API endpoints and base URL to Cognee Cloud.
- **0.1.0**: Initial release with Add Data, Cognify, and Search operations.

## License

MIT
