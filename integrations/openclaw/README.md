# @cognee/cognee-openclaw

OpenClaw plugin that adds Cognee-backed memory with automatic recall and indexing.

## Features

- **Auto-recall**: Before each agent run, searches Cognee for relevant memories and injects them as context
- **Auto-index**: On startup and after each agent run, syncs memory markdown files to Cognee (add new, update changed, delete removed, skip unchanged)
- **Per-agent datasets**: Route each OpenClaw agent to a different Cognee dataset via `datasetNames`
- **CLI commands**: `openclaw cognee index` to manually sync, `openclaw cognee status` to check state
- **Configurable**: Search type, max results, score filtering, token limits, and more

## Why Per-Agent Datasets

When multiple agents share one OpenClaw gateway, storing all long-term memory in a single Cognee dataset can leak memories across agents. That breaks the intended boundary between agent sessions and long-term memory.

This integration keeps the original single-dataset behavior by default, but also lets users decide when agents should stay isolated and when they should intentionally share memory.

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
openclaw plugins install @cognee/cognee-openclaw
```

## Configuration

Enable the plugin in your OpenClaw config (`~/.openclaw/config.yaml` or project config):

```yaml
plugins:
  slots:
    memory: cognee-openclaw
  entries:
    cognee-openclaw:
      enabled: true
      config:
        baseUrl: "http://localhost:8000"
        apiKey: "${COGNEE_API_KEY}"
        datasetName: "openclaw"
        datasetNames:
          asst: "asst-dataset"
          lawyer: "lawyer-dataset"
          lexi: "lexi"
        searchType: "GRAPH_COMPLETION"
        deleteMode: "hard"
        maxResults: 6
        autoRecall: true
        autoIndex: true
```

In `datasetNames`, each key is an OpenClaw agent id and each value is the Cognee dataset name for that agent. Integrations that also attach vaults should make the vault-side `cognee.datasetName` match one of those agent dataset names or the shared default `datasetName`.

### Dataset Routing Patterns

Use `datasetName` as the default fallback dataset, then override only the agents that need different memory boundaries.

Mixed example:

```yaml
plugins:
  slots:
    memory: cognee-openclaw
  entries:
    cognee-openclaw:
      enabled: true
      config:
        datasetName: "shared-default"
        datasetNames:
          asst: "shared-default"
          lawyer: "legal-memory"
          lexi: "media-memory"
          elena: "shared-default"
```

This gives you three common patterns in one config:

- `lawyer` uses an exclusive dataset.
- `lexi` uses an exclusive dataset.
- `asst` and `elena` explicitly share the default dataset.

Any agent not listed in `datasetNames` also falls back to `datasetName`, which preserves the original single-dataset behavior. For example, if an agent such as `researcher` is enabled under the same gateway but does not appear in `datasetNames`, it will still use `shared-default`.

Set your API key in the environment:

```bash
export COGNEE_API_KEY="your-key-here"
```

### Dev/Prod parity

Use the same plugin id and config shape in both local development and production:

1. Development (local path):

```bash
cd integrations/openclaw
npm install
npm run build
openclaw plugins install -l .
```

2. Production (registry package):

```bash
openclaw plugins install @cognee/cognee-openclaw
```

Both use:

- plugin id: `cognee-openclaw`
- config key: `plugins.entries.cognee-openclaw`
- memory slot: `plugins.slots.memory: cognee-openclaw`

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `baseUrl` | string | `http://localhost:8000` | Cognee API base URL |
| `apiKey` | string | `$COGNEE_API_KEY` | API key for authentication |
| `datasetName` | string | `openclaw` | Default dataset name used when an agent id does not have an entry in `datasetNames` |
| `datasetNames` | object | `{}` | Per-agent dataset overrides keyed by agent id; values are Cognee dataset names |
| `searchType` | string | `GRAPH_COMPLETION` | Search mode: `GRAPH_COMPLETION`, `CHUNKS`, `SUMMARIES` |
| `maxResults` | number | `6` | Max memories to inject per recall |
| `minScore` | number | `0` | Minimum relevance score filter |
| `maxTokens` | number | `512` | Token cap for recall context |
| `autoRecall` | boolean | `true` | Inject memories before agent runs |
| `autoIndex` | boolean | `true` | Sync memory files on startup and after agent runs |
| `autoCognify` | boolean | `true` | Run cognify after new memories are added |
| `deleteMode` | string | `soft` | Delete mode: `soft` removes raw data only, `hard` also removes degree-one graph nodes |
| `searchPrompt` | string | `""` | System prompt sent to Cognee to guide search query processing |
| `requestTimeoutMs` | number | `60000` | HTTP timeout for Cognee requests |
| `ingestionTimeoutMs` | number | `300000` | HTTP timeout for add/update (ingestion) requests, which are typically slower |

## How It Works

1. **On startup**: Scans `memory/` directory for markdown files and syncs to Cognee (add new, update changed, delete removed, skip unchanged)
2. **Before agent start**: Searches Cognee for memories relevant to the prompt and prepends as `<cognee_memories>` context
3. **After agent end**: Re-scans memory files and syncs any changes the agent made (including deletions)
4. **State tracking**:
   - `~/.openclaw/memory/cognee/datasets.json` — dataset ID mapping
  - `~/.openclaw/memory/cognee/sync-index.json` — per-dataset sync index (`byDataset`) with per-file hashes and Cognee data IDs

Memory files detected at: `MEMORY.md` and `memory/**/*.md` (recursive)

## CLI Commands

```bash
# Manually sync memory files to Cognee
openclaw cognee index

# Check sync status (indexed files, pending changes)
openclaw cognee status
```

## Development

```bash
cd integrations/openclaw
# Build once, then link for local development with an OpenClaw project
npm install
npm run build
openclaw plugins install -l .
```

For live rebuilds during development:

```bash
npm run dev
```

## Testing

Run the test suite to verify functionality:

```bash
npm test
```
