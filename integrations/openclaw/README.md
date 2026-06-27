# @cognee/cognee-openclaw

OpenClaw plugin that adds Cognee-backed memory with **multi-scope support** (company/user/agent), session tracking, and automatic recall.

## Features

- **Multi-scope memory**: Separate datasets for company-wide knowledge, per-user preferences, and per-agent context
- **Scope-aware routing**: Memory files are automatically routed to the correct dataset based on directory structure
- **Multi-scope recall**: Before each agent run, searches across all configured scopes and injects labeled context
- **Session tracking**: Multi-turn conversation context via Cognee's session system
- **14 search types**: From simple semantic search (CHUNKS) to chain-of-thought graph reasoning (GRAPH_COMPLETION_COT) to auto-selection (FEELING_LUCKY)
- **Health check**: Verifies Cognee API connectivity before operations
- **Auto-index**: Syncs memory markdown files to Cognee via `/remember` (add new, update changed, forget removed, skip unchanged). The `/remember` endpoint runs ingest, graph build, and graph enrichment in one server-side call.
- **In-session memory**: each recall call auto-captures the turn as a `QAEntry` in Cognee's session cache; with `AUTO_FEEDBACK=true` set on the Cognee container, follow-up messages are auto-classified as feedback and attached to the previous QA; `session_end` triggers `/improve` to bridge the session cache into the graph
- **One-command setup**: `openclaw cognee setup` configures Cognee as the sole memory provider
- **CLI commands**: `openclaw cognee setup`, `openclaw cognee index`, `openclaw cognee status`, `openclaw cognee health`, `openclaw cognee scopes`, `openclaw cognee forget`, `openclaw cognee improve`

## Security: Recommended Plugin Allowlist

OpenClaw will auto-load any plugin it discovers if `plugins.allow` is not set. To restrict which plugins can load, add an explicit allowlist to your `~/.openclaw/openclaw.json`:

```yaml
plugins:
  allow:
    - cognee-openclaw
    - cognee-openclaw-skills
```

Without this, any plugin found in your environment could be loaded automatically.

## Installation

Install the plugin locally for development:

```bash
cd integrations/openclaw
npm install
npm run build
openclaw plugins install -l .
```

Or once published:

```bash
# Pin to an exact version to avoid unintended updates (supply-chain best practice)
openclaw plugins install @cognee/cognee-openclaw@2026.3.0
```

## Quick Start

After installing, run the setup command to configure Cognee as the memory provider:

```bash
# Cognee only (replaces built-in memory)
openclaw cognee setup

# Or keep built-in memory enabled in config
openclaw cognee setup --hybrid
```

**Default mode** disables built-in memory providers — all recall comes from Cognee.

**Hybrid mode** keeps `memory-core` enabled in config, but on OpenClaw versions with exclusive memory slots only the slot winner loads at runtime.
This plugin registers its own memory flush plan, so pre-compaction flush works when Cognee owns the memory slot.

Then configure the Cognee connection in `~/.openclaw/openclaw.json`:

```yaml
plugins:
  entries:
    cognee-openclaw:
      enabled: true
      hooks:
        allowConversationAccess: true   # see note below
      config:
        baseUrl: "http://localhost:8000"
        apiKey: "${COGNEE_API_KEY}"
        datasetName: "my-project"
```

> `hooks.allowConversationAccess` -> OpenClaw ≥ 2026.4.27 blocks non-bundled plugins from registering the `agent_end` hook unless this flag is set. Without it, file sync memory operations after each agent turn is silently disabled. The gateway still loads the plugin, but file changes the agent makes won't reach Cognee until the next manual `openclaw cognee index` or gateway start. Restart the gateway after adding the flag: `openclaw gateway stop && openclaw gateway start`.

## Modes

The plugin connects to cognee in one of three modes. It picks the mode
automatically from your config:

| Mode | When it's used | How it talks to cognee |
| --- | --- | --- |
| **local-server** (default) | no `COGNEE_BASE_URL`, `COGNEE_EMBEDDED` unset | ensures a local cognee server is running and connects as a thin client |
| **remote** | `COGNEE_BASE_URL` is set | thin client to your managed / cloud cognee |
| **embedded** | `COGNEE_EMBEDDED=true` | runs cognee in-process |

**Why local-server is the default.** cognee's local stores (SQLite, Kuzu/Ladybug,
LanceDB) are single-writer. Driving them in-process from the agent's background
threads — or from a second process sharing the same `data_root` — risks
`database is locked` errors and corruption. A local cognee server is the single
owner that serializes all access, so the agent just makes HTTP calls.
**`embedded` is opt-in and is safe for single-process / offline use only.**

**No silent fallbacks.** The plugin never downgrades modes behind your back. If
`COGNEE_BASE_URL` is set but unreachable, or the local server fails to start,
initialization raises rather than quietly switching to a different mode — silent
fallback would either mask a config error (remote → local data divergence) or
reintroduce the very DB-lock risk this design removes (local-server → embedded).
To accept the single-process trade-off, set `COGNEE_EMBEDDED=true` explicitly.

### Cognee Cloud

To use Cognee Cloud instead of a local instance, set `mode` to `"cloud"`:

```yaml
plugins:
  entries:
    cognee-openclaw:
      enabled: true
      config:
        mode: "cloud"
        baseUrl: "https://tenant-xxx.cloud.cognee.ai/api"
        apiKey: "${COGNEE_API_KEY}"
```

Or via environment variables:

```bash
export COGNEE_MODE=cloud
export COGNEE_BASE_URL=https://tenant-xxx.cloud.cognee.ai/api
export COGNEE_API_KEY=your-api-key
```

**Cloud mode supported operations**: `remember` (new files), `recall`, per-item `forget`. The `/remember` and `/recall` endpoints are verified against self-hosted Cognee 1.0.3; cloud parity for these specific routes has not been validated yet — file an issue if you hit a 404.

**Known limitation**: Updating existing data (modifying a previously synced file) is not supported in cloud mode — `PATCH /update` is self-hosted only. In cloud mode the plugin's update path is a no-op; to edit a single file in place, delete it and re-add it via the [Cognee Cloud platform](https://platform.cognee.ai) or the API directly.

## Multi-Scope Memory

For production use, enable multi-scope mode by setting any scope-specific dataset name:

```yaml
plugins:
  entries:
    cognee-openclaw:
      enabled: true
      config:
        baseUrl: "http://localhost:8000"
        apiKey: "${COGNEE_API_KEY}"

        # Multi-scope datasets
        companyDataset: "acme-shared"
        userDatasetPrefix: "acme-user"
        agentDatasetPrefix: "acme-agent"
        userId: "${OPENCLAW_USER_ID}"
        agentId: "code-assistant"

        # Search all scopes during recall (in priority order)
        recallScopes:
          - agent
          - user
          - company

        # Default scope for files not matching any route
        defaultWriteScope: "agent"
```

### Memory Scope Hierarchy

| Scope | Dataset | Purpose | Example Files |
|-------|---------|---------|---------------|
| **Company** | `acme-shared` | Shared knowledge across all users/agents | `memory/company/policies.md`, `memory/company/domain-glossary.md` |
| **User** | `acme-user-alice` | Per-user preferences, feedback, corrections | `memory/user/preferences.md`, `memory/user/feedback.md` |
| **Agent** | `acme-agent-code-assistant` | Per-agent learned behaviors, tool outputs | `memory/tools.md`, `MEMORY.md` |

### Scope Routing

Files are routed to scopes based on their path. Default routing rules:

```
memory/company/**  ->  company scope
memory/user/**     ->  user scope
memory/**          ->  agent scope (catch-all)
MEMORY.md          ->  agent scope
```

Custom routing via config:

```yaml
scopeRouting:
  - pattern: "memory/shared/**"
    scope: company
  - pattern: "memory/personal/**"
    scope: user
  - pattern: "memory/**"
    scope: agent
```

### Multi-Scope Recall

During recall, the plugin searches each scope independently and injects labeled results:

```xml
<cognee_memories>
  <agent_memory>[agent-specific results]</agent_memory>
  <user_memory>[user preference results]</user_memory>
  <company_memory>[shared knowledge results]</company_memory>
</cognee_memories>
```

This lets the agent distinguish between personal context, shared knowledge, and its own learned patterns.

## Configuration Options

### Connection

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `baseUrl` | string | `http://localhost:8000` | Cognee API base URL |
| `apiKey` | string | `$COGNEE_API_KEY` | API key for authentication |

### Memory Scopes

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `companyDataset` | string | — | Dataset for company-wide memory. Setting this enables multi-scope mode |
| `userDatasetPrefix` | string | — | Prefix for user datasets (becomes `{prefix}-{userId}`) |
| `agentDatasetPrefix` | string | — | Prefix for agent datasets (becomes `{prefix}-{agentId}`) |
| `userId` | string | `$OPENCLAW_USER_ID` | User identifier for user-scoped memory |
| `agentId` | string | `default` | Agent identifier for agent-scoped memory |
| `recallScopes` | string[] | `["agent","user","company"]` | Scopes to search during recall, in priority order |
| `defaultWriteScope` | string | `agent` | Default scope for files not matching any route |
| `scopeRouting` | object[] | (see above) | Path-to-scope routing rules |

### Sessions

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enableSessions` | boolean | `true` | Enable session-based conversation tracking |
| `persistSessionsAfterEnd` | boolean | `true` | Persist session Q&A into the knowledge graph |

### Search

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `searchType` | string | `GRAPH_COMPLETION` | Search strategy (see below) |
| `maxResults` | number | `3` | Max memories to inject per scope (sent as `top_k` to Cognee) |
| `minScore` | number | `0.3` | Minimum relevance score filter |
| `searchPrompt` | string | `""` | System prompt to guide search |
| ~~`maxTokens`~~ | number | `512` | **Deprecated** — Cognee 1.0.3's recall payload no longer accepts a token cap; use `maxResults` instead. Setting this is silently ignored. |

### Search Types

| Type | Description |
|------|-------------|
| `GRAPH_COMPLETION` | **Default** — graph traversal + LLM reasoning |
| `CHUNKS` | Semantic vector search, returns raw stored text (no generation) |
| `FEELING_LUCKY` | Auto-selects a strategy per query (may pick generative modes) |
| `GRAPH_COMPLETION_COT` | Chain-of-thought reasoning over graph (iterative) |
| `GRAPH_COMPLETION_CONTEXT_EXTENSION` | Extended context retrieval (multiple rounds) |
| `GRAPH_SUMMARY_COMPLETION` | Graph with pre-computed summaries |
| `RAG_COMPLETION` | Traditional RAG with document chunks |
| `TRIPLET_COMPLETION` | Subject-predicate-object search |
| `CHUNKS` | Pure semantic vector search |
| `CHUNKS_LEXICAL` | Keyword/lexical search |
| `SUMMARIES` | Pre-computed hierarchical summaries |
| `TEMPORAL` | Time-aware graph search |
| `NATURAL_LANGUAGE` | Natural language to graph query |
| `CYPHER` | Direct graph database queries |
| `CODING_RULES` | Code-specific rule search |

### Automation

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `autoRecall` | boolean | `true` | Inject memories before agent runs |
| `autoIndex` | boolean | `true` | Sync memory files on startup, after agent runs, and on session end |
| `improveOnSessionEnd` | boolean | `true` | On `session_end`, call `/improve` with the session id to bridge session-cache QAs into the graph |
| ~~`autoCognify`~~ | boolean | `true` | **Deprecated** — `/remember` runs the cognify step server-side. Setting this is silently ignored. |
| ~~`autoMemify`~~ | boolean | `false` | **Deprecated** — graph enrichment now runs server-side via `/remember`'s `self_improvement` default. Setting this is silently ignored. |
| ~~`deleteMode`~~ | string | `soft` | **Deprecated** — `/forget` always runs the equivalent of `soft`; the legacy `hard` mode is gone (cognee's source explicitly warns against it). Setting this is silently ignored. |

### Timeouts

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `requestTimeoutMs` | number | `60000` | HTTP timeout for Cognee requests |
| `ingestionTimeoutMs` | number | `300000` | HTTP timeout for add/update requests |

Note: Files are stored in Cognee using sanitized relative paths as filenames (e.g., `MEMORY.md.txt` for `MEMORY.md`, `memory.tools.md.txt` for `memory/tools.md`) for easy identification and to avoid path separator issues.

## CLI Commands

```bash
# Configure Cognee as the memory provider (run once after install)
openclaw cognee setup              # Cognee only
openclaw cognee setup --hybrid     # Keep built-ins enabled in config (runtime co-load depends on slot rules)

# Manually sync memory files to Cognee
openclaw cognee index

# Check sync status (files indexed, dataset info, per-scope breakdown)
openclaw cognee status

# Verify Cognee API connectivity
openclaw cognee health

# Show memory scope routing for current workspace files
openclaw cognee scopes

# Wipe a dataset, or all of this user's data, from Cognee
openclaw cognee forget --dataset <name>
openclaw cognee forget --everything --confirm

# Bridge captured QAs (and any feedback) into the permanent graph
openclaw cognee improve                       # current dataset, all sessions
openclaw cognee improve --session-id <id>     # scope to one session
```

## How It Works

1. **On startup**: Health check, then scan `memory/` directory and call `/api/v1/remember` (one batched multipart upload per scope). Cognee runs add + cognify + improve server-side.
2. **Before each prompt**: Call `/api/v1/recall` for each configured scope in parallel, merge results with scope labels, inject as `<cognee_memories>` context. The openclaw session id is passed through; Cognee uses it to auto-capture the turn as a session QA and (with `AUTO_FEEDBACK=true`) auto-attach feedback to the prior QA when one is detected.
3. **After each agent run**: Re-scan memory files; new files batch into `/remember`, changed files go through `PATCH /update` (self-hosted) with fallback to `/remember`, removed files are dropped via `/forget`.
4. **On session end**: Final sync sweep. With `improveOnSessionEnd` on, also dispatches `/improve` for the just-ended session id to bridge session QAs into the permanent graph.
5. **State tracking**:
   - `~/.openclaw/memory/cognee/datasets.json` — dataset ID mapping
   - `~/.openclaw/memory/cognee/scoped-sync-indexes.json` — per-scope file hashes and data IDs
   - `~/.openclaw/memory/cognee/sync-index.json` — legacy single-scope index

Memory files detected at: `MEMORY.md` and `memory/**/*.md` (recursive)

## Development

```bash
cd integrations/openclaw
npm install
npm run build
openclaw plugins install -l .
```

For live rebuilds during development:

```bash
npm run dev
```

## Testing

```bash
npm test
```
