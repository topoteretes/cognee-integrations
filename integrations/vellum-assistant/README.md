# Cognee Plugin for Vellum Assistant

Cognee knowledge graph memory for Vellum Assistant. Session-aware storage, auto-routing recall, and persistent learning across sessions. Supports local mode (self-hosted Cognee server) and Cognee Cloud.

## Architecture

This is a **pure TypeScript** plugin — no Python, no subprocess. All logic runs in-process under Bun, using Bun's native `fetch` for HTTP calls to the Cognee API.

### File layout

```
vellum-assistant/
  package.json              # Vellum plugin manifest (peer dep @vellumai/plugin-api ^0.10.3)
  src/
    cognee-client.ts         # HTTP client: recall, remember, agent registration, circuit breaker
    plugin-common.ts         # Config, session mapping, logging, bridge cache, API key resolution
    bridge.ts                # Session resolution helpers (conversationId → Cognee session)
    session-start.ts         # Init logic: backend check, API key minting, agent registration
    session-context-lookup.ts # Recall for auto-context injection (session + trace + graph)
    store-to-session.ts      # Store tool calls (TraceEntry) and QA pairs (QAEntry)
    store-user-prompt.ts     # Stage user prompt for pairing with assistant response
    sync-session-to-graph.ts # Bridge session cache → permanent graph (dedup by hash)
    post-compact.ts          # Build memory anchor after context compaction
    exit-watcher.ts          # Background: final sync when parent process exits
    idle-watcher.ts          # Background: sync idle sessions
  hooks/
    init.ts                  # Plugin init: disable Vellum default memory, resolve backend
    user-prompt-submit.ts    # Auto-recall + stage prompt
    post-tool-use.ts         # Store tool calls as TraceEntries
    stop.ts                  # Pair prompt+response as QAEntry, auto-sync threshold
    post-compact.ts          # Inject memory anchor after compaction
    shutdown.ts              # Final graph sync + unregister agent
  tools/
    cognee-recall.ts         # Model-visible tool for explicit memory search
  skills/
    cognee-remember/         # Skill: store data in permanent graph
    cognee-search/           # Skill: search memory (uses cognee_recall tool)
    cognee-sync/             # Skill: manual session-to-graph sync
  tests/
    test_cognee_client.py    # Tests for cognee-client.ts (ported from Python)
    test_memory_preference.py
    test_recall_http.py
    test_remember_http.py
    test_session_id.py
```

### Hook mapping

| Hook | Fires | What it does |
|------|-------|-------------|
| `init` | Plugin load | Disables Vellum default memory (config.json + .disabled sentinels), resolves backend, mints API key if local |
| `user-prompt-submit` | Each user turn | Auto-recalls relevant context from Cognee, injects into messages, stages prompt |
| `post-tool-use` | After each tool call | Stores tool call as TraceEntry in session cache |
| `stop` | Turn end | Pairs staged prompt with assistant response as QAEntry, triggers graph sync if threshold reached |
| `post-compact` | After compaction | Pulls memory anchor (recent QAs, trace, graph context), injects into compacted history |
| `shutdown` | Plugin unload | Final graph sync, unregisters agent connection |

### Disabling Vellum's default memory

The `init` hook disables Vellum's built-in memory system so Cognee is the sole memory provider:

1. **Config flags**: Writes `memory.enabled = false` and `memory.v2.enabled = false` to `<workspace>/config.json`. The daemon's config cache auto-invalidates on file change.

2. **Default plugin sentinels**: Creates `.disabled` sentinel files at:
   - `<workspace>/plugins/default-memory-retrieval/.disabled`
   - `<workspace>/plugins/default-memory-v3-shadow/.disabled`

This works because user plugin `init` hooks run **before** `bootstrapPlugins()` checks the `.disabled` sentinels for default plugins.

### Circuit breaker

Recall calls go through a file-based circuit breaker (`~/.cognee-plugin/recall-breaker.json`). After 5 consecutive failures (UNREACHABLE or 5xx), the breaker opens for 120 seconds. A reachable 4xx (auth error) does NOT trip the breaker — waiting won't fix a config problem.

### Session management

The host session key (Vellum `conversationId`) maps to a deterministic Cognee session ID via first-writer-wins file creation at `~/.cognee-plugin/vellum-assistant/sessions/<hostKey>.json`. A separate per-launch `conn_uuid` is the registration/liveness handle.

### Plugin directory

The plugin is installed at `$VELLUM_WORKSPACE_DIR/plugins/cognee/`. All state lives under `~/.cognee-plugin/` (shared: API key cache, server-ready marker, circuit breaker) and `~/.cognee-plugin/vellum-assistant/` (per-session: config, logs, session maps, bridge cache).

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `COGNEE_BASE_URL` | `http://localhost:8011` | Cognee server URL |
| `COGNEE_API_KEY` | (none) | API key for the Cognee server |
| `COGNEE_PLUGIN_DATASET` | `agent_sessions` | Dataset name for storage |
| `COGNEE_AGENT_NAME` | `vellum-assistant` | Agent name for session IDs |
| `COGNEE_SESSION_PREFIX` | `vellum` | Session ID prefix |
| `COGNEE_BREAKER_THRESHOLD` | `5` | Failures before circuit opens |
| `COGNEE_BREAKER_COOLDOWN` | `120` | Seconds before retry after circuit opens |

### Config file

`~/.cognee-plugin/vellum-assistant/config.json` — created on first init, can be edited manually:

```json
{
  "mode": "local",
  "base_url": "http://localhost:8011",
  "api_key": "",
  "dataset": "agent_sessions",
  "agent_name": "vellum-assistant",
  "session_prefix": "vellum",
  "auto_improve_every": 30
}
```

## Cognee server

If using local mode, the Cognee server must be running at the configured `COGNEE_BASE_URL` (default `http://localhost:8011`). The plugin does not start the server itself — it expects one to already be running, either:

- A local Cognee server (`cognee serve` or the Cognee Docker image)
- A Cognee Cloud instance (set `COGNEE_BASE_URL` to your cloud URL)

If the server is unreachable, all hooks degrade gracefully (no-ops) and the circuit breaker prevents hammering.

### LLM API key (required for graph sync)

The `/api/v1/remember` endpoint (used for session-to-graph sync) runs Cognee's cognify pipeline, which requires an LLM API key on the server. Without it, graph sync will fail with `LLMAPIKeyNotSetError`.

Session memory (`/api/v1/remember/entry` for QA pairs and traces) does **not** require an LLM key and works without one.

To configure the LLM key on the Cognee server:

```bash
# Via the settings API
curl -X POST http://localhost:8011/api/v1/settings \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: <key>" \
  -d '{"llm_api_key":"sk-..."}'

# Or via environment variable on the server process
export LLM_API_KEY=sk-...
```

The init hook checks for an LLM key and logs a warning if none is configured.

## API key resolution

1. `COGNEE_API_KEY` env var (highest priority)
2. Cached key at `~/.cognee-plugin/api_key.json` (minted on first init for local servers)
3. For local servers with no key: the init hook mints one via `/api/v1/auth/login` + `/api/v1/auth/api-keys`

## Diff from Claude Code integration

This integration is adapted from the [Claude Code cognee plugin](../claude-code/). Key differences:

| Aspect | Claude Code | Vellum Assistant |
|--------|-------------|-------------------|
| Language | Python scripts + shell wrappers | Pure TypeScript (Bun) |
| Hooks | JSON-configured subprocess hooks | TypeScript hooks (in-process) |
| Manifest | `.claude-plugin/plugin.json` + `hooks/hooks.json` | `package.json` |
| Tools | Agent definition (markdown) | `ToolDefinition` (TypeScript) |
| Memory disabling | N/A | Disables Vellum default memory via config + sentinels |
| Plugin dir | `~/.claude/plugins/` | `$VELLUM_WORKSPACE_DIR/plugins/cognee/` |
| Session key | Claude session ID | Vellum `conversationId` |
| Subprocess | Yes (Python via stdin/stdout JSON) | No (all in-process) |
