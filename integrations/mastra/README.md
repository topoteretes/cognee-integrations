<div align="center">
  <a href="https://www.cognee.ai">
    <img src="https://raw.githubusercontent.com/topoteretes/cognee-integrations/main/assets/cognee-logo.svg" alt="Cognee" width="260">
  </a>
  <p><strong>Cognee as a knowledge-graph memory layer for Mastra agents</strong> — automatic recall/capture processors plus explicit recall tools, over cognee's HTTP API.</p>
  <p>
    <a href="https://docs.cognee.ai">Docs</a> ·
    <a href="https://discord.gg/NQPKmU5CCg">Discord</a> ·
    <a href="https://github.com/topoteretes/cognee">Cognee core</a> ·
    <a href="https://mastra.ai">Mastra</a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license">
    <img src="https://img.shields.io/badge/npm-not%20yet%20published-lightgrey" alt="npm: not yet published">
  </p>
</div>

# @cognee/cognee-mastra

Cognee knowledge-graph memory for [Mastra](https://mastra.ai) agents. Two surfaces, both optional and usable together:

- **Processors** — `createCogneeProcessors()` / `withCognee()` recall from cognee before every turn and capture the turn afterwards, automatically.
- **Tools** — `createCogneeTools()` gives the model `cognee_search`, `cognee_ask`, `cognee_remember`, and (opt-in) `cognee_forget` so it can query and write memory explicitly.

Both talk to a cognee server over its plain HTTP API — no cognee Python SDK, no embedded server, no additional runtime dependency beyond `@mastra/core` and `zod`.

> **npm package name in this document (`@cognee/cognee-mastra`) is a plan, not a live fact.** This package has not been published as of `0.1.0` — see [Compatibility](#compatibility) and the `registry` field in `integrations/inventory.yml`.

## What this is / what this is not

This package is **additive**. It sits *alongside* your existing Mastra `Memory` (libsql, postgres, whatever you already use as the message store of record) and adds a semantic/graph layer on top of it. It never replaces `Memory`, and it never touches the messages `Memory` stores.

**It is not a `MastraMemory` or `MemoryStorage` implementation**, and that is deliberate, not an oversight. `MemoryStorage` is a message-CRUD contract: callers expect `listMessages`/`saveMessages`/`updateMessages`/`cloneThread` etc. to return messages addressed by a stable id, with ordered pagination and byte-exact round-tripping of tool-call parts. Cognee's HTTP API is an ingest-and-retrieve knowledge graph (`/remember`, `/recall`, `/datasets`, `/forget`) — there is no message-by-id endpoint, no ordered pagination, and no update-message endpoint. Implementing `MemoryStorage` on top of that would mean faking a message table inside a graph store, and a `cognify` pass can rewrite/normalize ingested text in ways that would silently corrupt the agent's own conversation history. Every other Mastra memory vendor that ships today (Mem0, Supermemory, Zep) made the same call and shipped processors and/or tools instead — including Zep, which *does* have native threads/messages and still didn't route through `MemoryStorage`.

Also out of scope for `0.1.0`:

- **Observational / working memory** (`getWorkingMemory` and friends) — not implemented, not stubbed.
- **Streaming recall** — the input processor recalls once, before the model call; it does not stream partial results.
- **A thread-end hook.** Mastra's core (as of the pinned `@mastra/core` version) exposes no `onThreadEnd`/`onSessionEnd` lifecycle event a processor can observe, so cognee's `/improve` (which promotes the session cache into the permanent graph) has no automatic trigger. `improveOnThreadEnd` therefore does not exist as a config flag — call `client.improve({ dataset_id, session_ids })` yourself if you want it, e.g. on your own app's session-close event.

## Requirements

- Node ≥ 20 (declared in `package.json#engines`).
- `@mastra/core` — peer range `>=1.64.0`, the version this package was built and tested against. The true lower bound is probably older; see [Compatibility](#compatibility).
- A reachable cognee server. No hard version floor is declared for `0.1.0` — every endpoint this package depends on is behind a one-shot runtime capability probe (see [Failure behaviour](#failure-behaviour)) that degrades to an older route shape automatically. Tested end-to-end against `cognee/cognee:main` reporting `1.5.4-local` (see [Compatibility](#compatibility)).

## Install

```bash
npm install @cognee/cognee-mastra @mastra/core
```

(`@mastra/core` is a peer dependency — install it yourself, matching the version your agent already uses.)

## Quick start

```ts
import { Agent, type AgentConfig } from "@mastra/core/agent";
import { Memory } from "@mastra/memory";
import { withCognee } from "@cognee/cognee-mastra";

// Declare the config as a typed constant rather than an inline literal:
// withCognee() is generic over the config you pass, and an inline literal
// makes TypeScript infer the narrow constraint and reject id/name/model as
// excess properties.
const baseAgentConfig: AgentConfig = {
  id: "assistant",
  name: "assistant",
  instructions: "You are a helpful assistant with long-term memory.",
  model: "openai/gpt-4o-mini",
  memory: new Memory(), // required — see the note below
};

const agent = new Agent(withCognee(baseAgentConfig, { dataset: "my-app" }));
```

**This one thing has to be true or the processors silently do nothing:** cognee's processors only run once the agent has *some* Mastra `Memory` attached (even a bare, unconfigured one is enough — it's needed purely so Mastra plumbs a `thread`/`resourceId` through), and every `generate()`/`stream()` call passes `memory: { thread, resource }`. This isn't a limitation this package adds; it's how Mastra's own `parseMemoryRequestContext(requestContext)` works — with no `Memory` and no `thread`/`resourceId` for the call, it returns `null`, and both `CogneeInputProcessor` and `CogneeOutputProcessor` no-op (fail open, not fail loud — see [Failure behaviour](#failure-behaviour)). Installing this package alone, with no `Memory` and no ids passed per call, does nothing observable.

```ts
await agent.generate("What did we decide about the launch date?", {
  memory: { thread: "thread-123", resource: "user-42" },
});
```

## Run cognee locally

The fastest path is Docker, cognee's embedded SQLite + LanceDB + Kuzu stack, no external services:

```yaml
services:
  cognee:
    image: cognee/cognee:main
    ports: ["8000:8000"]
    environment:
      LLM_API_KEY: ${LLM_API_KEY}
```

```bash
LLM_API_KEY=sk-... docker compose -f examples/docker-compose.smoke.yml up
curl http://localhost:8000/health
```

The stock image ships with a built-in user, `default_user@example.com` / `default_password`, and (as tested on `cognee/cognee:main` 1.5.4) enforces authentication on every API route regardless of `REQUIRE_AUTHENTICATION`, so point the client at those credentials for local work:

```bash
export COGNEE_USER_EMAIL=default_user@example.com
export COGNEE_USER_PASSWORD=default_password
```

No OpenAI key handy? An offline variant works with local models — set these on the same service instead of `LLM_API_KEY`:

```yaml
    environment:
      LLM_PROVIDER: ollama
      LLM_MODEL: ollama_chat/llama3.1
      EMBEDDING_PROVIDER: fastembed
```

Credentials are never optional for this client: with neither `apiKey` nor `auth.email`/`auth.password` configured, the first authenticated request fails locally (the processors then no-op and the tools return `{ error }`), so the default-user pair above is the minimum even for a throwaway local server. Past local development, switch to `COGNEE_API_KEY` or a real user's `COGNEE_USER_EMAIL`/`COGNEE_USER_PASSWORD`.

## Usage

### Processors — automatic recall and capture

```ts
import { createCogneeProcessors } from "@cognee/cognee-mastra";

const { input, output } = createCogneeProcessors({ dataset: "my-app" });

const agent = new Agent({
  name: "assistant",
  model: "openai/gpt-4o-mini",
  memory: new Memory(),
  inputProcessors: [input],
  outputProcessors: [output],
});
```

- **Input** (`CogneeInputProcessor.processInput`) runs before every model call. It takes the last user message (truncated to 2000 characters), skips recall entirely if it's shorter than `recall.minQueryLength` (default 8 — filters out "ok", "yes", …), and otherwise calls `recall()` with `search_type: CHUNKS`, `only_context: true`. A hit set becomes one system message, added via `messageList.addSystem(block, "cognee")`:

  ```
  Relevant memory from cognee:
  1. The launch date was moved to March 14th. (source: chunk)
  2. Priya owns the release checklist. (source: chunk)
  ```

  No hits, a query below the length floor, a tripped circuit breaker, or any error → the message list is returned unmodified. The turn is never blocked or failed because of cognee.

- **Output** (`CogneeOutputProcessor.processOutputResult`) runs once, after the full generation result is available. It builds a `{question, answer}` pair from the turn (question comes from the new user message(s) in the turn unless `write.mode: "assistant-only"`, answer from the model's reply) and writes it via `rememberEntry()` — **fire-and-forget**: the call is never awaited on the response path, so a slow or failing cognee write adds zero latency and never fails the turn.

- `withCognee(agentConfig, config)` does both at once: it appends cognee's input processor to whatever `inputProcessors` you already have and its output processor to whatever `outputProcessors` you already have (function or array form, either works), rather than replacing them.

### Tools — explicit, model-driven recall and writes

```ts
import { createCogneeTools } from "@cognee/cognee-mastra";

const agent = new Agent({
  name: "assistant",
  model: "openai/gpt-4o-mini",
  tools: createCogneeTools({ dataset: "my-app" }),
});
```

| Tool | Input | Output | What it does |
|---|---|---|---|
| `cognee_search` | `{ query, topK?, nodeName? }` | `{ results?: Hit[], error? }` | `search_type: CHUNKS`, `only_context: true`. Fast, no LLM call inside cognee. Default recommendation for "what do we know about X". |
| `cognee_ask` | `{ question, topK? }` | `{ answer?, references?: Hit[], error? }` | `search_type: GRAPH_COMPLETION`, `only_context: false`, `include_references: true`. Reasons over the graph and returns synthesized prose. **Slow and expensive** — see [Search types](#search-types). |
| `cognee_remember` | `{ statement, metadata? }` | `{ success?, status?, error? }` | Writes one fact via `remember()`. `metadata` entries fold into `node_set` tags (`key:value`, sanitized). |
| `cognee_forget` *(opt-in, `tools.enableForget: true`)* | `{ dataId }` | `{ success?, error? }` | Deletes one memory item, `memory_only: true` always forced — `everything: true` can never be sent from this tool, hard-blocked in `CogneeClient.forget()` itself too. |

`Hit` is `{ text, score?, source?, datasetId?, datasetName? }`.

Every tool's `execute` is wrapped in `context.observe.span("cognee.<id>", …)` and **never throws** — a cognee failure comes back as `{ error: "..." }` (still matching the declared output schema) so the model can read and react to it instead of the tool call opaquely failing. `cognee_forget` is present in `createCogneeTools()`'s returned record **only** when `config.tools.enableForget === true` (env `COGNEE_ENABLE_FORGET`); the standalone `createCogneeForgetTool()` export is never gated, for a caller who wants it without opting the whole collection in.

### Both together (recommended)

```ts
import { createCogneeProcessors, createCogneeTools, CogneeClient } from "@cognee/cognee-mastra";

const client = new CogneeClient({ dataset: "my-app" });
const { input, output } = createCogneeProcessors({ client });
const tools = createCogneeTools({ client });

const agent = new Agent({
  name: "assistant",
  model: "openai/gpt-4o-mini",
  memory: new Memory(),
  inputProcessors: [input],
  outputProcessors: [output],
  tools,
});
```

Passing the same `CogneeClient` instance to both keeps one dataset-id cache and one JWT/API-key session instead of two. **Watch the overlap**: if the output processor is writing every turn *and* the model calls `cognee_remember`, the same fact can land twice — cognee's own graph normalization is the intended place to absorb that (this package does not deduplicate), but if you'd rather avoid it outright, set `write.mode: "never"` and rely on the tool alone, or the reverse (skip the tool, keep the processor).

## Configuration reference

Resolution order for every field: **explicit argument → environment variable → default**, all through one function, `resolveConfig(partial, env?)`. No field is read from `process.env` anywhere else in the package.

| Key | Env var | Default | Effect |
|---|---|---|---|
| `baseUrl` | `COGNEE_API_URL` | `http://localhost:8000` | cognee server base URL. |
| `apiKey` | `COGNEE_API_KEY` | *(none)* | Sent as `X-Api-Key`. Wins over `auth` when both are set. |
| `auth.email` | `COGNEE_USER_EMAIL` | *(none)* | Used only when `apiKey` is absent. |
| `auth.password` | `COGNEE_USER_PASSWORD` | *(none)* | Used only when `apiKey` is absent. |
| `dataset` | `COGNEE_DATASET` | `mastra` | Dataset name under `scope: "tagged"` (the default). |
| `datasetPrefix` | `COGNEE_DATASET_PREFIX` | `mastra_` | Prefix used by `scope: "dataset-per-resource"`. |
| `scope` | `COGNEE_SCOPE` | `tagged` | `"tagged"` or `"dataset-per-resource"` — see [Scoping and multi-tenancy](#scoping-and-multi-tenancy). |
| `nodeSet` | *(none)* | `[]` | Extra `node_set` tags applied to every write, alongside the resource/thread tags. |
| `recall.enabled` | `COGNEE_RECALL_ENABLED` | `true` | Set `false` to disable the input processor's recall call entirely (tools are unaffected). |
| `recall.searchType` | `COGNEE_SEARCH_TYPE` | `CHUNKS` | Search strategy for the *automatic* recall path. See [Search types](#search-types). |
| `recall.topK` | `COGNEE_TOP_K` | `10` | Max passages/nodes to return. |
| `recall.minQueryLength` | *(none — code only)* | `8` | Recall is skipped for a query shorter than this (filters "ok", "yes", …). |
| `recall.timeoutMs` | `COGNEE_RECALL_TIMEOUT_MS` | `2500` | Per-call timeout for the recall request. Setting this (it always has a value) also disables client-side retry for that call — the recall path never retries. |
| `recall.budgetMs` | `COGNEE_RECALL_BUDGET_MS` | `4000` | Wall-clock cap the input processor itself enforces, independent of `timeoutMs` — a hung server can add at most `budgetMs` (+~100ms) to a turn, never more. |
| `recall.includeReferences` | *(none — code only)* | `true` | Passed as `include_references` on `recall()`. |
| `write.mode` | `COGNEE_SAVE_MODE` | `always` | `"always"` \| `"assistant-only"` (skip capturing the user's question) \| `"never"` (output processor becomes a no-op). |
| `write.runInBackground` | *(none — code only)* | `true` | Passed to `remember()`/`add()`. Has no effect on the output processor's own write, which goes through `rememberEntry()` — that endpoint has no `run_in_background` field. |
| `write.maxChars` | *(none — code only)* | `8000` | Per-message truncation cap (word-boundary) before a turn is serialized for cognee. |
| `tools.enableForget` | `COGNEE_ENABLE_FORGET` | `false` | Gates `cognee_forget` into (or out of) `createCogneeTools()`'s returned record. |
| `tools.timeoutMs` | `COGNEE_TOOLS_TIMEOUT_MS` | `10000` | Per-call budget for every `cognee_search`/`cognee_ask`/`cognee_remember`/`cognee_forget` tool call. Setting `timeoutMs` per call also disables retry for that call unless a `retries` override is also given (`client.ts`'s `RequestOptions`) — reads (`cognee_search`/`cognee_ask`) get 0 retries at this budget, writes (`cognee_remember`/`cognee_forget`) keep 1. Without this, a tool call inherited `requestTimeoutMs` × up to `retries + 1` attempts — ~141s worst case for one model-facing tool call. |
| `requestTimeoutMs` | `COGNEE_TIMEOUT_MS` | `30000` | Default per-request timeout for a call that sets no per-call `timeoutMs` of its own — the processors' recall/write paths (which set `recall.timeoutMs`) and the tool calls above (which set `tools.timeoutMs`) don't use this; it applies to `CogneeClient` calls made directly through the exported escape hatch, and to `ensureDataset`/`listDatasets`/etc. |
| `retries` | `COGNEE_RETRIES` | `3` | Retry count on a 5xx/429/network failure, exponential backoff (base 3s: 3s, 6s, 12s, …). Disabled whenever a per-call `timeoutMs` is set. |
| `debug` | `COGNEE_DEBUG` | `false` | Logs `METHOD path -> status (Nms, attempt K)` per request. Never logs headers, bodies, `apiKey`, or `password` — see [Failure behaviour](#failure-behaviour). |
| `fetch` | *(none — code only)* | global `fetch` | Injectable, for tests. |

`createCogneeProcessors()`/`createCogneeTools()`/`new CogneeClient()` never throw at construction time for a missing credential — the client throws on the first request that actually needs to authenticate, so it's always safe to build these at module scope before any credential exists.

## Scoping and multi-tenancy

Two modes, set via `scope` (default `"tagged"`):

- **`tagged`** (default) — one dataset for the whole integration (`config.dataset`, default `"mastra"`). Every write tags `node_set: ["resource:<resourceId>", "thread:<threadId>", ...config.nodeSet]`. Every automatic recall filters with `node_name: ["resource:<resourceId>"]`.
- **`dataset-per-resource`** — one dataset per resource, named `${datasetPrefix}${resourceId}` (default prefix `mastra_`), created lazily via `ensureDataset()` and cached in memory for the process lifetime. Not the default: it multiplies datasets, and each one needs its own cognify pipeline run.

> **`node_name` filtering is retrieval scoping only.** It changes which memories a query is filtered against; anyone holding valid cognee credentials can still call `/recall` directly with a different `node_name` filter, or none at all. Whether `node_set`/`node_name` carry any RBAC semantics on the server side is undocumented in cognee.
>
> **If you have more than one real tenant behind one cognee server, `scope: "tagged"` does not isolate them.** Real isolation requires `scope: "dataset-per-resource"` **and** per-tenant cognee credentials (a dataset a credential cannot see is a much stronger boundary than a tag a query happens not to ask for). Do not market or rely on `scope: "tagged"` as multi-tenant.

## Search types

cognee exposes many `search_type` values; this package only chooses defaults for two paths and documents a third:

| `search_type` | Used by | Cost | When |
|---|---|---|---|
| `CHUNKS` | Automatic recall (default), `cognee_search` | No LLM call inside cognee — vector/keyword lookup only. | Default. The input processor runs on *every* turn, so a per-turn hidden LLM call and its latency would be an unacceptable surprise; injected context should be evidence (raw passages), not a second model's prose. |
| `GRAPH_COMPLETION` | `cognee_ask` only | Triggers an LLM call **inside the cognee server**, billed to the cognee server's own API key — not this conversation's. Slower (typically several seconds). | Only when the model (or you, directly via the tool) actually needs graph reasoning and a synthesized written answer, not a passage lookup. |
| `TEMPORAL` | Neither, by default | Same class of cost as `GRAPH_COMPLETION` if it triggers reasoning; not exercised by this package's defaults. | Set `recall.searchType: "TEMPORAL"` yourself for time-aware queries ("what changed since last week") — cognee's own docs cover the semantics; this package passes the value through untouched. |

Every automatic-path default is overridable via `recall.searchType`; the type itself is a plain string, not a closed enum, so any value the server accepts (`SUMMARIES`, `RAG_COMPLETION`, `CYPHER`, `AGENTIC_COMPLETION`, …) can be set without waiting on this package to catch up.

## Failure behaviour

- **Fail open, always.** A cognee failure in either processor never propagates to the agent turn — every network call in `CogneeInputProcessor.processInput` and `CogneeOutputProcessor.processOutputResult` sits inside a try/catch whose failure path is "return the message list unchanged" (input) or "silently stop" (output, already fire-and-forget). Tools are the one place a cognee failure is visible to anyone — as a `{ error: string }` return value the model can read, never a thrown error.
- **Recall has a hard wall-clock budget** (`recall.budgetMs`, default 4000ms) enforced by the processor itself, independent of and on top of the client's own per-call `recall.timeoutMs` (default 2500ms) — a hung server can add at most `budgetMs` (+~100ms) to a turn, never more, regardless of what caused the delay.
- **The recall path never retries.** Every recall call sets a per-call `timeoutMs`, and this client disables its retry loop whenever a per-call `timeoutMs` is present — a slow recall fails fast once, it does not compound the latency with 3 retries.
- **Tool calls (`cognee_search`/`cognee_ask`/`cognee_remember`/`cognee_forget`) also get a bounded per-call budget** (`tools.timeoutMs`, default 10000ms), not the client-wide `requestTimeoutMs`/`retries` defaults — a bare tool call would otherwise inherit up to `requestTimeoutMs` × (`retries` + 1) attempts (~141s worst case, default settings) before returning to the model. Reads get 0 retries at this budget; writes keep 1.
- **A file-backed circuit breaker guards the recall path only** (not writes), and only counts failures that actually indicate cognee is unhealthy: a 5xx response or a network/timeout failure with no response at all. A deterministic 4xx (bad API key, a 422 validation error, a `baseUrl` pointed at a server with no matching route) never counts — five of those mean "this config is broken," not "cognee is down," and must not also blackout recall for 120 seconds on top of the config problem. 5 consecutive *breaker-eligible* failures open it for 120 seconds; while open, the input processor skips the network call entirely (checked before any request is made) and returns the message list unmodified. State lives at `~/.cognee-mastra/recall-breaker.json`; a missing, unreadable, or corrupt state file degrades to *closed* (never fails open by accident because its own bookkeeping broke), and any error message persisted there has credential-shaped substrings (`apiKey: ...`, `password=...`, …) redacted before it's written.
- **Writes have no breaker.** A slow or failing write already degrades to "silently dropped, try again next turn" — the correct failure mode for a best-effort, fire-and-forget side channel, with no user-facing latency for a breaker to protect.
- **`debug: true` never logs a secret.** Debug output is `METHOD path -> status (Nms, attempt K)` only — no headers, no request/response bodies, so a login form's password and the bearer token it returns can never reach a log line. `redactConfigForLogging()` masks `apiKey`/`auth.password` for any caller that wants to log the resolved config itself.
- **Runtime capability probing, not a version check.** Whether `/recall` or the legacy `/search` answers, whether `/remember` exists or writes must go through `/add`+`/cognify`, whether `/remember/entry` exists, and which auth path prefix is mounted — none of this is assumed; the client tries the modern route first, falls back once on a 404, and caches the winner per base URL for the life of the process. A 404 costs one extra round trip, once, ever, per base URL.

## Compatibility

- **cognee server**: talks only to the plain HTTP API — `/health`, `/api/v1/auth/login`, `/api/v1/datasets` (+ `/status`), `/api/v1/recall` (falls back to `/api/v1/search`), `/api/v1/remember` (falls back to `/api/v1/add` + `/api/v1/cognify`), `/api/v1/remember/entry` (falls back to `/api/v1/remember`), `/api/v1/forget`, `/api/v1/improve`. No cognee Python SDK dependency, no embedded server. No hard minimum server version is declared for `0.1.0` — the capability probes above are the compatibility mechanism. **Tested server version: `cognee/cognee:main` reporting `1.5.4-local`** (live smoke runs, 2026-09-07: health → ensureDataset → remember with a `session_id` → wait → recall → forget, green on three consecutive runs). What that server actually does with a session-mode write, as observed: `/remember` answers `status: "session_stored"` at once; `/datasets/status?pipeline=cognify_pipeline` returns `{}` for the dataset (no run yet), then `DATASET_PROCESSING_STARTED`, then `DATASET_PROCESSING_COMPLETED` as a background bridge cognifies the session cache into the graph; the text was recallable via `CHUNKS` after ~17–25s. Probes resolved on those runs: `recallPath: /api/v1/recall`, `rememberSupported: true` (`/remember` exists and, for session writes, cognify follows in the background), `authPrefix: /api/v1/auth`. The delay is an observation, not a guarantee: the write path is eventually consistent, and a server that bridges sessions differently may never show a `cognify_pipeline` run for the dataset at all, which is why the live test treats recall, not the status poll, as its success criterion. Two live findings worth knowing: that server enforces auth regardless of `REQUIRE_AUTHENTICATION` (JWT login as the built-in default user is the stock path; arbitrary `X-Api-Key` values are rejected), and it rejects `raw_data` writes whenever `content_type` is anything but `'code'` — which is why this client never sends `content_type` for plain text.
- **`@mastra/core`**: peer range `>=1.64.0`. Verified directly against the real `Processor` interface (`id` required, `name` optional/display-only; four distinct output hooks — `processOutputStream`, `processOutputResult`, `processOutputStep`, `processToolResult` — of which this package uses only `processOutputResult`), `parseMemoryRequestContext`, and `inputProcessors`/`outputProcessors` wiring, both at the installed `1.64.0` devDependency and against a newer pinned source tree (`1.65.0-alpha.7`) — identical in both. The true lower bound is plausibly older (`processOutputResult` gained its `result` argument at core `1.11.0` per that package's own CHANGELOG) but was not bisected — narrowing the floor later is always a safe, non-breaking change; this package will not accidentally need to move it up in a patch release.
- **Node**: ≥ 20.

## Development

```bash
npm install
npm run build          # tsc
npm run typecheck      # tsc --noEmit --skipLibCheck
npm test               # jest, unit + e2e tiers, no network, no live server
npm run test:unit      # config / scope / format / breaker / errors
npm run test:e2e       # processors / tools against an in-process mock cognee server
npm run test:live      # COGNEE_LIVE=1 — opt-in, requires a real reachable cognee server; never run in CI
```

`jest.setup.ts` redirects `os.homedir()` to a temp directory for every test file, so the circuit breaker's state file never touches a real developer `~` during a test run.

The mock server (`__tests__/test-utils/mock-cognee.ts`) is a real `node:http` server on an ephemeral port, not a fetch monkey-patch or `nock` — the client's actual timeout/retry/abort behaviour runs against it unmodified.

## Limitations / roadmap

- No observational memory or working-memory bridge (`getWorkingMemory` and related methods) — out of scope for `0.1.0`, not stubbed.
- No streaming recall — the input processor recalls once per turn, before the model call.
- `improve()` (session cache → permanent graph) is not called automatically on any schedule or lifecycle event — call it manually via the exported `CogneeClient` if you want it.
- Duplicate writes are possible (and not deduplicated by this package) when both the output processor and `cognee_remember` are active for the same conversation — see [Both together](#both-together-recommended).
- `scope: "tagged"` is explicitly not a multi-tenant isolation boundary — see [Scoping and multi-tenancy](#scoping-and-multi-tenancy).
- Not yet published to npm as of `0.1.0` — `registry: none` in `integrations/inventory.yml`.

## License / attribution

MIT — see [`LICENSE`](./LICENSE). The HTTP client's retry, auth and timeout behaviour follows `integrations/openclaw`'s `CogneeHttpClient` in this repo.
