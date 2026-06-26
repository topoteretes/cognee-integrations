# @cognee/cognee-openclaw

OpenClaw plugin that adds Cognee-backed memory with **multi-scope support** (company/user/agent), session tracking, and automatic recall.

## Features

- **Multi-scope memory**: Separate datasets for company-wide knowledge, per-user preferences, and per-agent context
- **Scope-aware routing**: Memory files are automatically routed to the correct dataset based on directory structure
- **Multi-scope recall**: Before each agent run, searches across all configured scopes and injects labeled context
- **Session tracking**: Multi-turn conversation context via Cognee's session system
- **Agent lifecycle registration**: Registers/unregisters each agent session with the Cognee server on every prompt turn; combined with `COGNEE_AGENT_MODE=true` on the server, Cognee shuts down automatically when all agents disconnect
- **14 search types**: From simple semantic search (CHUNKS) to chain-of-thought graph reasoning (GRAPH_COMPLETION_COT) to auto-selection (FEELING_LUCKY)
- **Lazy dataset resolution**: On first prompt, if a dataset UUID is not cached locally, the plugin queries the Cognee server by name so you can connect to any pre-existing dataset without manual configuration
- **Health check**: Verifies Cognee API connectivity before operations
- **Auto-index**: Syncs memory markdown files to Cognee via `/remember` (add new, update changed, forget removed, skip unchanged). The `/remember` endpoint runs ingest, graph build, and graph enrichment in one server-side call.
- **In-session memory**: Each recall call auto-captures the turn as a `QAEntry` in Cognee's session cache; with `AUTO_FEEDBACK=true` set on the Cognee container, follow-up messages are auto-classified as feedback and attached to the previous QA; `session_end` triggers `/improve` to bridge the session cache into the graph
- **One-command setup**: `openclaw cognee setup` configures Cognee as the sole memory provider
- **CLI commands**: `openclaw cognee setup`, `openclaw cognee index`, `openclaw cognee status`, `openclaw cognee health`, `openclaw cognee scopes`, `openclaw cognee forget`, `openclaw cognee improve`

## Security: Recommended Plugin Allowlist

OpenClaw will auto-load any plugin it discovers if `plugins.allow` is not set. To restrict which plugins can load, add an explicit allowlist to your `~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "allow": ["cognee-openclaw"]
  }
}
```

> **Important**: `plugins.allow` must be a JSON **array**, not an object. `{"cognee-openclaw": true}` is invalid and will cause a config parse error.

Without this, any plugin found in your environment could be loaded automatically.

## Installation

### Published package

```bash
# Pin to an exact version to avoid unintended updates (supply-chain best practice)
openclaw plugins install @cognee/cognee-openclaw@2026.3.0
```

### Development install (symlink)

When developing or modifying the plugin, install as a symlink so that `npm run build` takes effect immediately without reinstalling:

```bash
cd integrations/openclaw
npm install
npm run build
openclaw plugins install --link .
```

> **Why `--link`?** A standard install copies the built files once. Any subsequent `npm run build` updates the source but not the installed copy — so OpenClaw keeps running the stale version. With `--link`, the installed path **is** the source directory, so every build is reflected on the next gateway start.

After install, verify the install entry in `~/.openclaw/openclaw.json`:

```json
"installs": {
  "cognee-openclaw": {
    "source": "path",
    "sourcePath": "/path/to/integrations/openclaw",
    "installPath": "/path/to/integrations/openclaw"
  }
}
```

`sourcePath === installPath` confirms the symlink is in place.

## Quick Start

After installing, run the setup command to configure Cognee as the memory provider:

```bash
# Cognee only (replaces built-in memory)
openclaw cognee setup

# Or keep built-in memory enabled in config
openclaw cognee setup --hybrid
```

**Default mode** disables built-in memory providers — all recall comes from Cognee.

**Hybrid mode** keeps `memory-core` enabled in config, but on OpenClaw versions with exclusive memory slots only the slot winner loads at runtime. This plugin registers its own memory flush plan, so pre-compaction flush works when Cognee owns the memory slot.

Then configure the Cognee connection in `~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "allow": ["cognee-openclaw"],
    "entries": {
      "cognee-openclaw": {
        "enabled": true,
        "hooks": {
          "allowPromptInjection": true
        },
        "config": {
          "baseUrl": "http://localhost:8011",
          "datasetName": "agent_sessions"
        }
      },
      "memory-core": { "enabled": false },
      "memory-lancedb": { "enabled": false }
    },
    "slots": {
      "memory": "cognee-openclaw"
    }
  }
}
```

> **`hooks.allowPromptInjection: true` is required.** Without it, OpenClaw blocks the plugin from reading the prompt content in the `before_prompt_build` hook — recall is silently skipped and no memories are injected. This key was named `allowConversationAccess` in versions before OpenClaw 2026.4.2; the old key is silently rejected, so if you copied config from an older guide, update to `allowPromptInjection`. Restart the gateway after adding or changing the flag.

### Multi-Agent Quick Start

For a gateway with multiple named agents sharing a default dataset:

```json
{
  "plugins": {
    "allow": ["cognee-openclaw"],
    "entries": {
      "cognee-openclaw": {
        "enabled": true,
        "hooks": { "allowPromptInjection": true }
      },
      "memory-core": { "enabled": false },
      "memory-lancedb": { "enabled": false }
    },
    "slots": { "memory": "cognee-openclaw" }
  },
  "auth": {
    "profiles": {
      "openai:manual": { "provider": "openai", "mode": "token" }
    }
  },
  "models": {
    "providers": {
      "openai": {
        "baseUrl": "https://api.openai.com/v1",
        "models": [{ "id": "gpt-4o-mini", "name": "GPT-4o mini" }]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": { "primary": "openai/gpt-4o-mini" },
      "models": { "openai/gpt-4o-mini": {} }
    },
    "list": [
      { "id": "Will", "name": "Will" },
      { "id": "Elizabeth", "name": "Elizabeth" }
    ]
  },
  "gateway": {
    "auth": { "mode": "token", "token": "your-gateway-token" }
  }
}
```

> **Required fields when adding a `models.providers.<provider>` block**: `baseUrl` is required by the config schema. Omitting it causes a validation error that prevents the gateway from starting.

By default, when `agents.list` has more than one entry, the plugin auto-enables `perAgentMemory` so each agent writes to its own dataset. Set `perAgentMemory: false` explicitly to share a single dataset across all agents.

### Cognee Cloud

To use Cognee Cloud instead of a local instance, set `mode` to `"cloud"`:

```json
{
  "plugins": {
    "entries": {
      "cognee-openclaw": {
        "enabled": true,
        "config": {
          "mode": "cloud",
          "baseUrl": "https://tenant-xxx.cloud.cognee.ai/api",
          "apiKey": "${COGNEE_API_KEY}"
        }
      }
    }
  }
}
```

Or via environment variables:

```bash
export COGNEE_MODE=cloud
export COGNEE_BASE_URL=https://tenant-xxx.cloud.cognee.ai/api
export COGNEE_API_KEY=your-api-key
```

**Cloud mode supported operations**: `remember` (new files), `recall`, per-item `forget`. The `/remember` and `/recall` endpoints are verified against self-hosted Cognee 1.0.3; cloud parity for these specific routes has not been validated yet — file an issue if you hit a 404.

**Known limitation**: Updating existing data (modifying a previously synced file) is not supported in cloud mode — `PATCH /update` is self-hosted only. In cloud mode the plugin's update path is a no-op; to edit a single file in place, delete it and re-add it via the [Cognee Cloud platform](https://platform.cognee.ai) or the API directly.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `COGNEE_BASE_URL` | `http://localhost:8011` | Cognee API base URL |
| `COGNEE_PLUGIN_DATASET` | `agent_sessions` | Dataset name for single-scope mode. Overridden by `datasetName` in config if set. Same variable name used by the claude-code and codex Cognee integrations. |
| `COGNEE_MODE` | `local` | `"local"` for self-hosted, `"cloud"` for Cognee Cloud |
| `COGNEE_API_KEY` | — | API key (cloud mode or authenticated self-hosted) |
| `COGNEE_USERNAME` | — | Login username (self-hosted with auth) |
| `COGNEE_PASSWORD` | — | Login password (self-hosted with auth) |
| `OPENCLAW_USER_ID` | — | User identifier for user-scoped memory |
| `OPENCLAW_AGENT_ID` | `default` | Agent identifier for agent-scoped memory |

## Multi-Scope Memory

For production use, enable multi-scope mode by setting any scope-specific dataset name:

```json
{
  "plugins": {
    "entries": {
      "cognee-openclaw": {
        "enabled": true,
        "config": {
          "baseUrl": "http://localhost:8011",
          "companyDataset": "acme-shared",
          "userDatasetPrefix": "acme-user",
          "agentDatasetPrefix": "acme-agent",
          "userId": "${OPENCLAW_USER_ID}",
          "agentId": "code-assistant",
          "recallScopes": ["agent", "user", "company"],
          "defaultWriteScope": "agent"
        }
      }
    }
  }
}
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

```json
{
  "scopeRouting": [
    { "pattern": "memory/shared/**", "scope": "company" },
    { "pattern": "memory/personal/**", "scope": "user" },
    { "pattern": "memory/**", "scope": "agent" }
  ]
}
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
| `baseUrl` | string | `http://localhost:8011` | Cognee API base URL (also: `COGNEE_BASE_URL`) |
| `apiKey` | string | `$COGNEE_API_KEY` | API key for authentication |
| `username` | string | `$COGNEE_USERNAME` | Login username |
| `password` | string | `$COGNEE_PASSWORD` | Login password |

### Dataset

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `datasetName` | string | `agent_sessions` | Dataset name for single-scope mode (also: `COGNEE_PLUGIN_DATASET`) |

### Memory Scopes

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `companyDataset` | string | — | Dataset for company-wide memory. Setting this enables multi-scope mode |
| `userDatasetPrefix` | string | — | Prefix for user datasets (becomes `{prefix}-{userId}`) |
| `agentDatasetPrefix` | string | — | Prefix for agent datasets (becomes `{prefix}-{agentId}`) |
| `agentDatasetTemplate` | string | — | Template for per-agent dataset with `{agentId}` placeholder; takes precedence over `agentDatasetPrefix` |
| `userId` | string | `$OPENCLAW_USER_ID` | User identifier for user-scoped memory |
| `agentId` | string | `default` | Agent identifier for agent-scoped memory (also: `OPENCLAW_AGENT_ID`) |
| `recallScopes` | string[] | `["agent","user","company"]` | Scopes to search during recall, in priority order |
| `defaultWriteScope` | string | `agent` | Default scope for files not matching any route |
| `scopeRouting` | object[] | (see above) | Path-to-scope routing rules |
| `perAgentMemory` | boolean | `false` (auto-`true` when `agents.list` > 1) | Give each agent its own dataset via `agentDatasetPrefix`. Auto-enabled for multi-agent gateways; set explicitly to `false` to force a shared dataset. |

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
| `maxTokens` | number | `512` | Token cap for recall per scope |
| `searchPrompt` | string | `""` | System prompt to guide search |
| `recallInjectionPosition` | string | `prependContext` | Where recalled memories are injected: `prependSystemContext`, `appendSystemContext`, or `prependContext` |

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
| ~~`autoCognify`~~ | boolean | `true` | **Deprecated** — `/remember` runs the cognify step server-side |
| ~~`autoMemify`~~ | boolean | `false` | **Deprecated** — graph enrichment now runs server-side via `/remember` |
| ~~`deleteMode`~~ | string | `soft` | **Deprecated** — `/forget` always runs soft delete |

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
openclaw cognee setup --hybrid     # Keep built-ins enabled in config

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

## Development

```bash
cd integrations/openclaw
npm install
npm run build
openclaw plugins install --link .
```

For live rebuilds during development:

```bash
npm run dev
```

After each build, restart the OpenClaw gateway to pick up the new code.

## Testing

```bash
npm test
```
