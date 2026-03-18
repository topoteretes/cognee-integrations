# @cognee/cognee-openclaw

OpenClaw plugin that adds Cognee-backed memory with **multi-scope support** (company/user/agent), session tracking, and automatic recall.

## Features

- **Multi-scope memory**: Separate datasets for company-wide knowledge, per-user preferences, and per-agent context
- **Scope-aware routing**: Memory files are automatically routed to the correct dataset based on directory structure
- **Multi-scope recall**: Before each agent run, searches across all configured scopes and injects labeled context
- **Session tracking**: Multi-turn conversation context via Cognee's session system
- **14 search types**: From simple semantic search (CHUNKS) to chain-of-thought graph reasoning (GRAPH_COMPLETION_COT) to auto-selection (FEELING_LUCKY)
- **Health check**: Verifies Cognee API connectivity before operations
- **Auto-index**: Syncs memory markdown files to Cognee (add new, update changed, delete removed, skip unchanged)
- **Memify support**: Optional graph enrichment after cognify for better entity consolidation
- **One-command setup**: `openclaw cognee setup` configures Cognee as the sole memory provider
- **CLI commands**: `openclaw cognee setup`, `openclaw cognee index`, `openclaw cognee status`, `openclaw cognee health`, `openclaw cognee scopes`

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
openclaw plugins install @cognee/cognee-openclaw@2026.3.1
```

## Quick Start

After installing, run the setup command to configure Cognee as the memory provider:

```bash
# Cognee only (replaces built-in memory)
openclaw cognee setup

# Or keep built-in memory alongside Cognee
openclaw cognee setup --hybrid
```

**Default mode** disables built-in memory providers — all recall comes from Cognee.

**Hybrid mode** keeps `memory-core` enabled — the agent uses both Cognee recall and built-in memory search.

Then optionally configure the Cognee connection in `~/.openclaw/openclaw.json`:

```yaml
plugins:
  entries:
    cognee-openclaw:
      enabled: true
      config:
        baseUrl: "http://localhost:8000"
        apiKey: "${COGNEE_API_KEY}"
        datasetName: "my-project"
```

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
| `apiKey` | string | `$COGNEE_API_KEY` | API key for Bearer token authentication |
| `username` | string | `$COGNEE_USERNAME` | Username for password-based login (used if no `apiKey`) |
| `password` | string | `$COGNEE_PASSWORD` | Password for password-based login (used if no `apiKey`) |
| `datasetName` | string | `openclaw` | Dataset name for single-scope mode |

### Memory Scopes

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `companyDataset` | string | — | Dataset for company-wide memory. Setting this enables multi-scope mode |
| `userDatasetPrefix` | string | — | Prefix for user datasets (becomes `{prefix}-{userId}`) |
| `agentDatasetPrefix` | string | — | Prefix for agent datasets (becomes `{prefix}-{agentId}`) |
| `userId` | string | `$OPENCLAW_USER_ID` | User identifier for user-scoped memory |
| `agentId` | string | `$OPENCLAW_AGENT_ID` / `default` | Agent identifier for agent-scoped memory |
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
| `maxResults` | number | `3` | Max memories to inject per scope |
| `minScore` | number | `0.3` | Minimum relevance score filter |
| `maxTokens` | number | `512` | Token cap for recall context per scope |
| `searchPrompt` | string | `""` | System prompt to guide search |

### Search Types

| Type | Description |
|------|-------------|
| `GRAPH_COMPLETION` | **Default** — graph traversal + LLM reasoning |
| `GRAPH_COMPLETION_COT` | Chain-of-thought reasoning over graph (iterative) |
| `GRAPH_COMPLETION_CONTEXT_EXTENSION` | Extended context retrieval (multiple rounds) |
| `GRAPH_SUMMARY_COMPLETION` | Graph with pre-computed summaries |
| `RAG_COMPLETION` | Traditional RAG with document chunks |
| `TRIPLET_COMPLETION` | Subject-predicate-object search |
| `CHUNKS` | Semantic vector search, returns raw stored text (no generation) |
| `CHUNKS_LEXICAL` | Keyword/lexical search |
| `SUMMARIES` | Pre-computed hierarchical summaries |
| `TEMPORAL` | Time-aware graph search |
| `NATURAL_LANGUAGE` | Natural language to graph query |
| `CYPHER` | Direct graph database queries |
| `CODING_RULES` | Code-specific rule search |
| `FEELING_LUCKY` | Auto-selects a strategy per query (may pick generative modes) |

### Automation

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `autoRecall` | boolean | `true` | Inject memories before agent runs |
| `autoIndex` | boolean | `true` | Sync memory files on startup and after agent runs |
| `autoCognify` | boolean | `true` | Run cognify after new memories are added |
| `autoMemify` | boolean | `false` | Run memify (graph enrichment) after cognify |
| `deleteMode` | string | `soft` | `soft` removes raw data, `hard` also removes graph nodes |

### Timeouts

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `requestTimeoutMs` | number | `60000` | HTTP timeout for Cognee requests |
| `ingestionTimeoutMs` | number | `300000` | HTTP timeout for add/update requests |

## CLI Commands

```bash
# Configure Cognee as the memory provider (run once after install)
openclaw cognee setup              # Cognee only
openclaw cognee setup --hybrid     # Cognee + built-in memory

# Manually sync memory files to Cognee
openclaw cognee index

# Check sync status (files indexed, dataset info, per-scope breakdown)
openclaw cognee status

# Verify Cognee API connectivity
openclaw cognee health

# Show memory scope routing for current workspace files
openclaw cognee scopes
```

## How It Works

1. **On startup**: Health check, then scan `memory/` directory and sync files to scope-specific Cognee datasets
2. **Before agent start**: Search each configured scope in parallel, merge results with scope labels, inject as `<cognee_memories>` context
3. **After agent end**: Re-scan memory files and sync any changes (including deletions) to the correct scope datasets
4. **State tracking**:
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
