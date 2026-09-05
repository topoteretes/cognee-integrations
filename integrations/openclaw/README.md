<div align="center">
  <a href="https://www.cognee.ai">
    <img src="https://raw.githubusercontent.com/topoteretes/cognee-integrations/main/assets/cognee-logo.svg" alt="Cognee" width="260">
  </a>
  <p><strong>Cognee memory for OpenClaw</strong> — persistent, multi-scope memory with automatic recall and capture for your OpenClaw agents.</p>
  <p>
    <a href="https://docs.cognee.ai">Docs</a> ·
    <a href="https://discord.gg/NQPKmU5CCg">Discord</a> ·
    <a href="https://github.com/topoteretes/cognee">Cognee core</a>
  </p>
  <p>
    <a href="https://www.npmjs.com/package/@cognee/cognee-openclaw"><img src="https://img.shields.io/npm/v/@cognee/cognee-openclaw" alt="npm version"></a>
    <a href="https://www.npmjs.com/package/@cognee/cognee-openclaw"><img src="https://img.shields.io/npm/dm/@cognee/cognee-openclaw" alt="npm downloads"></a>
  </p>
</div>

# @cognee/cognee-openclaw

OpenClaw plugin that adds Cognee-backed memory with **multi-scope support** (company/user/agent), session tracking, and automatic recall.

## Features

- **Multi-scope memory**: Separate datasets for company-wide knowledge, per-user preferences, and per-agent context
- **Scope-aware routing**: Memory files are automatically routed to the correct dataset based on directory structure
- **Multi-scope recall**: Before each agent run, searches across all configured scopes and injects labeled context
- **Session tracking**: Multi-turn conversation context via Cognee's session system
- **Agent lifecycle registration**: Registers/unregisters each agent session with the Cognee server on every prompt turn; combined with `COGNEE_AGENT_MODE=true` on the server, Cognee shuts down automatically when all agents disconnect
- **Search types**: Supports Cognee’s configured search types, including semantic search (CHUNKS), chain-of-thought graph reasoning (GRAPH_COMPLETION_COT), and automatic routing.
- **Lazy dataset resolution**: On first prompt, if a dataset UUID is not cached locally, the plugin queries the Cognee server by name so you can connect to any pre-existing dataset without manual configuration
- **Memory-hit visibility**: a `[cognee: N memories]` footer on replies where recall actually injected memories, plus a once-a-week digest of turns-with-hits and top sources — no extra LLM calls
- **Health check**: Verifies Cognee API connectivity before operations
- **Auto-index**: Syncs memory markdown files to Cognee via `/remember` (add new, update changed, forget removed, skip unchanged). The `/remember` endpoint runs ingest, graph build, and graph enrichment in one server-side call.
- **In-session memory**: Every tool call is stored as a `TraceEntry` and every prompt/answer pair as a `QAEntry` in Cognee's session cache (`captureSession`, on by default); with `AUTO_FEEDBACK=true` set on the Cognee container, follow-up messages are auto-classified as feedback and attached to the previous QA; `session_end` triggers `/improve` to bridge the session cache into the graph
- **Native memory tools**: registers `memory_search` and `memory_get` — the tools OpenClaw's memory slot and `active-memory` expect — backed by Cognee recall, with `cognee://` references the model can resolve to full text; plus `memory_forget` for user-directed, per-document deletion with mandatory confirmation, and `memory_switch_dataset` to move a conversation to another dataset
- **One-command setup**: `openclaw cognee setup` configures Cognee as the sole memory provider and sets the required hook permissions
- **Code graph**: `openclaw cognee index-repo <path|url>` indexes a repository into a deterministic code graph; `memory_code_search` answers callers/impact/path/endpoint questions exactly, and an identifier-gated recall lane injects code facts when a prompt names a symbol
- **Memory steer**: a cached system-prompt line on every run asserting Cognee as the authoritative memory and pointing the model at the memory tools
- **Version & update hint**: `openclaw cognee status` / `openclaw cognee version` show the installed version and, when npm has a newer release, how to upgrade
- **CLI commands**: `openclaw cognee setup`, `openclaw cognee index`, `openclaw cognee status`, `openclaw cognee version`, `openclaw cognee health`, `openclaw cognee scopes`, `openclaw cognee forget`, `openclaw cognee improve`

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
# Pin to an exact version to avoid unintended updates (supply-chain best practice).
# Check https://www.npmjs.com/package/@cognee/cognee-openclaw for the latest release.
openclaw plugins install @cognee/cognee-openclaw@2026.8.5
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

Both modes also write the plugin's hook permissions (`allowPromptInjection`, `allowConversationAccess`) into the plugin entry — see the note below for why both are needed.

Then configure the Cognee connection in `~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "allow": ["cognee-openclaw"],
    "entries": {
      "cognee-openclaw": {
        "enabled": true,
        "hooks": {
          "allowPromptInjection": true,
          "allowConversationAccess": true
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

> **Both `hooks` keys are required — they gate different hook groups.** `openclaw cognee setup` writes them for you; if you configure manually, set both:
>
> - **`allowConversationAccess: true`** — OpenClaw blocks *conversation hooks* (`llm_output`, `agent_end`) for installed (non-bundled) plugins unless this is explicitly `true`. Without it, Q&A session capture and post-agent memory sync are **silently skipped** — the only trace is a warning in gateway diagnostics.
> - **`allowPromptInjection: true`** — gates *prompt-injection hooks* (`before_prompt_build`), which the plugin uses to inject recalled memories. This one defaults to allowed, but set it explicitly: it also signals hook startup intent to OpenClaw's plugin activation.
>
> Earlier versions of this README claimed `allowConversationAccess` was renamed to `allowPromptInjection` in OpenClaw 2026.4.2 — that was wrong; the two keys coexist and both matter. Restart the gateway after adding or changing either flag.

### Multi-Agent Quick Start

For a gateway with multiple named agents sharing a default dataset:

```json
{
  "plugins": {
    "allow": ["cognee-openclaw"],
    "entries": {
      "cognee-openclaw": {
        "enabled": true,
        "hooks": { "allowPromptInjection": true, "allowConversationAccess": true }
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

#### Default: all agents share one dataset

By default — no matter how many agents are configured — every agent reads and writes the **same dataset** (`agent_sessions`), exactly like the claude-code and codex Cognee integrations. Agents stay distinguishable within it: each agent session registers separately and its conversation is keyed by its own Cognee session id, so recall and session bridging never mix sessions up. Shared memory is usually what you want: agents benefit from each other's knowledge.

#### Opt-in: per-agent isolated datasets

To give each agent its own dataset (own graph, own recall space), set **both** of these in the plugin config:

```json
"config": {
  "perAgentMemory": true,
  "agentDatasetPrefix": "myorg-agent"
}
```

With this, agent `Will` writes to dataset `myorg-agent-will`, agent `Elizabeth` to `myorg-agent-elizabeth`, and each agent's recall only searches its own dataset (plus any shared `company`/`user` scopes you configure). Use `agentDatasetTemplate` (e.g. `"{agentId}"`) instead of the prefix if you need full control over the dataset names.

> **Both settings are required.** `perAgentMemory: true` on its own does nothing — isolation only activates when an `agentDatasetPrefix` or `agentDatasetTemplate` is also set. Per-agent memory is never enabled automatically.

### Cognee Cloud

Cognee Cloud tenants (staging and production) serve the **same `/api/v1/*` API as a self-hosted server**, so connecting to the cloud is just the default configuration pointed at your tenant URL, with an API key:

```json
{
  "plugins": {
    "entries": {
      "cognee-openclaw": {
        "enabled": true,
        "hooks": { "allowPromptInjection": true, "allowConversationAccess": true },
        "config": {
          "baseUrl": "https://tenant-xxx.aws.cognee.ai",
          "apiKey": "${COGNEE_API_KEY}"
        }
      }
    }
  }
}
```

Or via environment variables:

```bash
export COGNEE_BASE_URL=https://tenant-xxx.aws.cognee.ai
export COGNEE_API_KEY=your-api-key
```

Do **not** set `mode: "cloud"` — leave it at the default. All operations (file sync, updates, recall, session capture, agent registration, improve) work against cloud tenants through the standard paths.

> **`COGNEE_API_KEY` is mandatory for any remote/cloud server.** On a local server the plugin auto-mints a key on first use (a one-time JWT login as the default user bootstraps the mint); remote servers expose no login route, so there is nothing to mint with — every request authenticates via `X-Api-Key`. The variable must be present in the environment the **gateway process** starts from — a daemonized gateway does not see `export`s from your current shell. Set it, then `openclaw gateway stop && openclaw gateway start`.

> **Deprecated: `mode: "cloud"` / `COGNEE_MODE=cloud`.** This mode targets a legacy path scheme (`baseUrl` ending `/api`, alias routes like `/recall` without the `/api/v1` prefix) that no current Cognee Cloud deployment serves — the platform control plane exposes no data routes, and tenants use the standard `/api/v1/*` paths. The mode is kept only for backward compatibility with older deployments; on current tenants it will 404. Newer capabilities (session capture, agent registration) are not implemented for the legacy scheme and never will be.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `COGNEE_BASE_URL` | `http://localhost:8011` | Cognee API base URL |
| `COGNEE_PLUGIN_DATASET` | `agent_sessions` | Dataset name for single-scope mode. Overridden by `datasetName` in config if set. Same variable name used by the claude-code and codex Cognee integrations. |
| `COGNEE_MODE` | `local` | **Deprecated** — leave at `"local"` for self-hosted *and* cloud tenants. `"cloud"` targets a legacy path scheme no current deployment serves (see "Cognee Cloud") |
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
        "hooks": { "allowPromptInjection": true, "allowConversationAccess": true },
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
| `perAgentMemory` | boolean | `false` | Give each agent its own dataset. Strictly opt-in (never auto-enabled); requires `agentDatasetPrefix` or `agentDatasetTemplate` to also be set — see "Multi-Agent Quick Start". By default all agents share one dataset. |

### Sessions

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enableSessions` | boolean | `true` | Enable session-based conversation tracking |
| `persistSessionsAfterEnd` | boolean | `true` | Persist session Q&A into the knowledge graph |
| `captureSession` | boolean | `true` | Store each tool call as a `TraceEntry` and each prompt/answer pair as a `QAEntry` in Cognee's session cache (requires `enableSessions`) |

### Search

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `searchType` | string | `HYBRID_COMPLETION` | Search strategy (see below) |
| `maxResults` | number | `3` | Max memories to inject per scope (sent as `top_k` to Cognee) |
| `minScore` | number | `0.3` | Minimum relevance score filter |
| `maxTokens` | number | `512` | Token cap for recall per scope |
| `searchPrompt` | string | `""` | System prompt to guide search |
| `recallInjectionPosition` | string | `prependContext` | Where recalled memories are injected: `prependSystemContext`, `appendSystemContext`, or `prependContext` |

### Code graph (repositories)

Cognee can index a whole repository into a deterministic **code graph** (the enola pipeline — no LLM or embedding calls) and answer structural questions exactly: who calls X, what breaks if X changes, how A reaches B, all routes. OpenClaw agents are rarely launched inside a checkout, so unlike the claude-code/codex plugins nothing is indexed automatically; the operator opts a repository in and the model gets a tool. Requires Cognee ≥ 1.5.3.

```bash
# Local path (the Cognee server must share this filesystem — the default local server does)
openclaw cognee index-repo ~/work/my-service --wait 60

# Git URL (the server clones it; the graph reflects PUSHED commits)
openclaw cognee index-repo https://github.com/org/repo --dataset codebase-repo --index-vectors
```

One narrow dataset per repository (`codebase-<repo>-<digest>` by default). `--index-vectors` also embeds the facts so `memory_search` can see them; without it the graph is reachable only through the code tool and lane. Indexed repositories are recorded in `~/.openclaw/memory/cognee/code-graphs.json`; re-run `index-repo` after changes (unchanged content is skipped server-side).

| Surface | What it does |
|---------|--------------|
| `memory_code_search` tool | `{query, operation?, args?, dataset?, limit?}` — operations `query_facts` (default, substring listing), `explore`, `traverse`, `find_path` (`args.source`/`args.target`), `impact_analysis`, `delta`. `dataset` is optional when exactly one repo is indexed |
| Code recall lane | When a prompt names an identifier-shaped token (backticked symbol, file path, `snake_case`, `CamelCase`, dotted name) **and** a code graph is indexed or listed in `codeDatasets`, one extra `scope: ["code"]` recall runs alongside the semantic lanes and its facts are injected as a `<code_graph>` block. Conversational prompts never trigger it |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `codeSearchTool` | boolean | `true` | Register `memory_code_search` |
| `codeGraphRecall` | boolean | `true` | Enable the identifier-gated code recall lane |
| `codeDatasets` | string[] | `[]` | Extra code-graph dataset names (e.g. indexed from another machine) |

### Memory steer

OpenClaw agents also have native memory files (`MEMORY.md`, `memory/*.md`) and may reach for them by habit. On every real agent run the plugin appends one static line to the system prompt (`appendSystemContext`, so providers cache it) asserting Cognee as the preferred, authoritative long-term memory and naming the memory tools — the counterpart of claude-code's `COGNEE_PREFER_MEMORY` steer. Heartbeat/cron turns are skipped.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `memorySteer` | boolean | `true` | Append the steer on each agent run |
| `memorySteerText` | string | built-in text | Replace the steer text entirely |

### Version & update check

`openclaw cognee status` leads with the installed plugin version, and `openclaw cognee version` prints it on its own. On gateway start the plugin refreshes a cached check against the npm registry (`~/.openclaw/memory/cognee/update-check.json`); when the cached latest is newer than the running version, both commands add `Update available: v… Run: openclaw plugins install @cognee/cognee-openclaw@latest`. The check is rate-limited and fail-silent — it never blocks a command or the gateway, and a network failure keeps the last known result. `--check-updates` forces a live check.

| Env var | Default | Description |
|---------|---------|-------------|
| `COGNEE_UPDATE_CHECK` | `true` | `false`/`0`/`no`/`off` disables the check |
| `COGNEE_UPDATE_CHECK_INTERVAL` | `86400` | Minimum seconds between background checks (same name as claude-code/codex) |

### Recall layers

Cognee holds more than the knowledge graph: every conversation also has a session cache with the captured Q&A turns, tool-call trace steps (with their feedback), and the agent guidance distilled from them by `/improve`. The server only searches those layers when the recall `scope` names them — with `dataset_ids`/`search_type` in the request, the default `auto` scope is graph-only. The plugin therefore runs one extra, cheap recall per prompt with `scope: ["session","trace","session_context"]` in parallel with the graph lanes and injects each non-empty layer as its own block:

```
<cognee_memories>
<agent_guidance>   … standing guidance from past sessions …
<trace_lessons>    … lessons from earlier tool calls …
<session_memory>   … earlier turns of this conversation …
<agent_memory> / <graph_memory> … knowledge-graph hits …
</cognee_memories>
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `recallSessionLayers` | boolean | `true` | Recall the session layers alongside the graph and inject them as separate sections. Requires `enableSessions` |

The same explicit scope backs `memory_search` with `corpus=sessions` / `all`.

### Agent tools: `memory_search` / `memory_get`

OpenClaw's memory slot comes with a tool contract: the bundled `active-memory` extension runs a memory sub-agent before conversational replies with exactly `memory_search` and `memory_get` allow-listed, and the model can call them directly. The plugin registers Cognee-backed versions of both (declared in `openclaw.plugin.json` → `contracts.tools`), so they work with no `toolsAllow` override and independently of `autoRecall`.

| Tool | Parameters | Returns |
|------|------------|---------|
| `memory_search` | `query` (required), `maxResults`, `minScore`, `corpus` = `memory` \| `sessions` \| `all` (default) | `{ results: [{ reference, text, score, scope, source, time }] }`; `{ results: [], disabled: true, error, warning, action }` when Cognee is unreachable or the recall breaker is open |
| `memory_get` | `path` (a `cognee://…` reference from `memory_search`, or a workspace memory file such as `MEMORY.md` / `memory/notes.md`), `from`, `lines` | The referenced memory's full text with provenance, or a bounded file excerpt with `truncated`/`nextFrom`. Stale references return an `error` field, not a failure |

`corpus=memory` searches the permanent graph across the configured scopes, `corpus=sessions` this conversation's session cache, `all` both. `wiki` is not backed by Cognee and returns no results. Set `memoryTools: false` to opt out.

### Agent tool: `memory_forget`

User-directed deletion — "forget what we talked about tennis", "delete that from memory". Deciding *which* stored documents match is a judgement the model makes by reading them, so the tool is two-phase and the model stays in the loop:

| Call | What happens |
|------|--------------|
| `memory_forget {action: "find", query: "tennis"}` | Lists the newest documents across the agent's datasets, reads each one's raw text (bounded scan of the 60 most recent), and returns candidates with `preview`, `sessionId` (when recoverable), `matchedTerms` and `dataId`/`datasetId`. Read-only. Pass `syncSession: true` to first bridge the current conversation into the graph so its content is findable |
| `memory_forget {action: "forget", dataIds: [...], confirm: true}` | Deletes exactly those documents, one `POST /api/v1/forget {datasetId, dataId}` each, and reports `deleted` / `failed`. Without `confirm: true` nothing is deleted and the tool asks for confirmation |

The tool deliberately cannot express a whole-dataset or everything-wipe; those stay behind `openclaw cognee forget --dataset <name>` / `--everything --confirm`. Deleting a document removes its raw data, its derived graph knowledge, and (Cognee ≥ 1.5.3) the session turns whose answers cited it — targeted, not whole-session; tool-call traces are not matched. Set `memoryForgetTool: false` to not register it.

### Agent tool: `memory_switch_dataset`

Move **one conversation** to another Cognee dataset — the OpenClaw counterpart of the claude-code/codex `cognee-switch-datasets` skill. One gateway serves many conversations per agent, so the switch is keyed by the host's `sessionKey` (falling back to `sessionId`), never by agent.

| Call | What happens |
|------|--------------|
| `{action: "list"}` | Datasets visible on the server, current one first. Present them and let the user pick; a name that is not listed is created on switch |
| `{action: "switch", dataset: "proj-a"}` | Syncs the current session into its dataset (`/improve`, strict — aborts on failure unless `force: true`), ensures the target exists and caches its id, then binds the conversation: later capture writes, the session-layer recall and the agent/single graph recall target `proj-a`, under a fresh Cognee session id (`open_claw_<id>__2`, `__3`, …) because a session never spans two datasets. Session-end `improve` follows too |
| `{action: "current"}` | The dataset and Cognee session id this conversation uses, and whether it was switched |
| `{action: "reset"}` | Back to the configured dataset. Re-syncs any retired session whose switch-time sync failed first; refuses without `force: true` while one is still unsynced |

`force: true` on a switch does not skip the sync — it defers it: the retired session is recorded on the override and bridged into its own dataset at session end (and by `reset`). Until then its turns exist only in the server's session cache, so the tool tells the model to inform the user.

In multi-scope mode only the **agent** scope is repointed; `company`/`user` memory stays shared. Memory-file sync keeps following `scopeRouting` — the switch moves the conversation's memory, not the agent's files. Overrides persist across gateway restarts in `~/.openclaw/memory/cognee/dataset-overrides.json`. Set `datasetSwitchTool: false` to not register it.

The plugin's bundled server pin is `cognee==1.5.3` (`src/server.ts`; the venv is upgraded automatically on next boot) and `cognee-docker-compose.yaml` uses `cognee/cognee:1.5.3`.

### Memory-hit visibility

Recall's cost (latency, injected tokens) is felt on every turn, but a recall that actually helped is invisible in the reply. Two features make it visible, at zero extra LLM calls and no hot-path I/O:

- **Per-turn footer** — when auto-recall injected at least one memory into a turn, the agent's *final* reply gets a one-line trailer, e.g. `[cognee: 3 memories]`. Turns with no hits (and heartbeat/cron turns, which never recall) get nothing. Streamed/tool chunks are never footered — only the final payload, once.
- **Weekly digest** — the plugin keeps a rolling 7-day count per agent of non-noise turns, turns with hits, and which memory sources produced them. When the week closes, the next final reply carries one summary line, e.g. `[cognee weekly digest] This week cognee found relevant memories on 47 of your agent's 120 turns (top sources: session summaries, MEMORY.md).` A week with zero hits posts nothing. Counters live in `~/.openclaw/memory/cognee/digest-stats.json`.

Both ride the outbound `reply_payload_sending` hook, which is not a conversation hook and therefore needs no `allowConversationAccess` grant.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `memoryHitFooter` | boolean | `true` | Append the footer on turns with ≥1 injected memory |
| `memoryHitFooterFormat` | string | `[cognee: {count} {memories}]` | Footer template. `{count}`, `{memories}` (memory/memories), `{sources}` (comma-joined source labels) |
| `weeklyDigest` | boolean | `true` | Append the weekly summary to the first final reply after a 7-day window closes |

### Harness-noise filter

OpenClaw drives agents with synthetic prompts the user never typed: heartbeat probes (`Read HEARTBEAT.md if it exists…`), cron payloads, and `System: …` event lines. Those are host instructions, not memory queries, so the plugin excludes them from auto-recall (which would otherwise run an LLM-backed search per scope, per heartbeat) and from QA capture (which would bridge the templates into the permanent graph via `/improve`). Filtering is two-layered: runs whose hook context carries a matching `trigger` are always filtered; prompts matching a shape pattern are filtered even without a trigger. Session registration and tool-call trace capture are unaffected.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `noiseTriggers` | string[] | `["heartbeat","cron"]` | `ctx.trigger` values treated as harness turns. `[]` disables this layer |
| `noisePatterns` | string[] | `["^Read HEARTBEAT\\.md", "^System:\\s", "^\\[cron\\b"]` | Regexes matched against the prompt (leading whitespace stripped). Replaces the defaults when set; `[]` disables this layer |

### Search Types

| Type | Description |
|------|-------------|
| `HYBRID_COMPLETION` | **Default** — combined vector + graph retrieval |
| `GRAPH_COMPLETION` | Graph traversal + LLM reasoning; slower but deeper — best for offline/CLI queries rather than the per-prompt recall path |
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
| `requestTimeoutMs` | number | `120000` | HTTP timeout for Cognee requests |
| `ingestionTimeoutMs` | number | `300000` | HTTP timeout for add/update requests |

### Recall budget & circuit breaker

Recall runs on the prompt hot path, so it is bounded: each recall call gets a short timeout, the whole recall step gets a wall-clock budget, and repeated failures open a circuit breaker that skips recall until the server recovers. Memories missed under the budget are dropped for that turn only — writes (traces, QA, file sync, improve) are never budgeted. The breaker state is shared with the claude-code and codex integrations via `~/.cognee-plugin/recall-breaker.json`, so all plugins using one Cognee server back off together.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `recallTimeoutMs` | number | `2500` | Per recall HTTP call timeout (no retries) |
| `recallBudgetMs` | number | `4000` | Overall wall-clock budget for the recall step per prompt |
| `recallBreakerThreshold` | number | `5` | Consecutive failures (network/timeout/5xx) before the breaker opens |
| `recallBreakerCooldownMs` | number | `120000` | How long recall is skipped once the breaker opens |

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
npm test              # everything hermetic — no server, no network, no cost
npm run test:unit         # pure logic + real-fs, no server
npm run test:integration   # the real HTTP client against a mock Cognee
npm run test:e2e           # the plugin's own lifecycle + CLI, via register()
npm run test:coverage
```

`npm test` is the whole contract for day-to-day work: it never reaches the
network and never spends anything.

### Tiers

| Tier | What it drives |
| --- | --- |
| `unit` | pure functions plus the filesystem modules (`files`, `persistence`) over real temp directories |
| `integration` | `CogneeHttpClient` against `MockCognee`, a real `node:http` server — the client calls global `fetch` with no injectable transport, so only a real socket exercises header assembly, the 401 re-login and timeouts |
| `e2e` | `register()` against a fake plugin API, then the nine lifecycle events and eight `cognee` subcommands fired at the collected handlers |
| `live` | a real Cognee server, real LLM calls, a real graph — **excluded from every default run** |

Shared helpers live in `test-utils/` rather than `__tests__/`, because jest's
default `testMatch` treats every file under `__tests__/` as a suite.

### The live tier

Opt in with a server you name explicitly:

```bash
COGNEE_RUN_LIVE=1 \
COGNEE_LIVE_BASE_URL=http://127.0.0.1:9100 \
COGNEE_LIVE_API_KEY=... \
npm run test:live
```

There is deliberately **no default URL**. A developer running this normally has a
real Cognee on the conventional port holding real data, and a tier that defaulted
to it would write into that graph. Naming the server is the consent. Each run
invents a `live_<uuid>` dataset and deletes only that namespace afterwards — never
delete-everything, because the target may hold real data.

Or let the plugin boot one, the way the nightly does — this exercises the real
first-run path (`ensure_and_boot.py`, the venv, the cognee pinned in
`src/server.ts`, uvicorn) and mints its own key:

```bash
COGNEE_RUN_LIVE=1 \
COGNEE_LIVE_ALLOW_BUILD=1 \
COGNEE_LIVE_BASE_URL=http://127.0.0.1:9100 \
LLM_API_KEY=... LLM_MODEL=openai/gpt-4o-mini \
npm run test:live
```

Only for a loopback URL nothing answers at. The venv is built under the jest
sandbox HOME by default, so it never touches your shared `~/.cognee-plugin/venv`;
set `COGNEE_LIVE_SERVER_HOME=/some/dir` to reuse one across runs (CI sets it to
the runner's home so the venv can be cached). `COGNEE_LIVE_VERBOSE=1` mirrors
the plugin's log lines to the console — the harness logger is otherwise a silent
`jest.fn()`, and a recall timeout with no plugin log is undiagnosable.

Two things that cost time to learn, both worth knowing before adding tests:

- **`os.homedir()` ignores `process.env.HOME` under jest.** Each test file gets its
  own `process.env`, and mutating it never reaches the C-level `getenv` libuv
  reads. `jest.setup.ts` therefore mocks `node:os` suite-wide so no test can write
  to the real `~/.openclaw` or `~/.cognee-plugin`. This exists because a CLI test
  overwrote a real `~/.openclaw/memory/cognee/` — per-file discipline had already
  failed once, so the default had to change.
- **Event field names are not interchangeable.** `after_tool_call` reads `toolName`
  (not `name`) and `llm_output` reads `assistantTexts` (not `text`). Get them wrong
  and the handlers simply find nothing to capture and return — the run goes green
  while storing nothing.
