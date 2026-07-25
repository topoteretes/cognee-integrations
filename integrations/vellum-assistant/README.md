# Cognee Plugin for Vellum Assistant

Cognee knowledge graph memory for Vellum Assistant. Session-aware storage, auto-routing recall, and persistent learning across sessions. Supports local mode (self-hosted Cognee server) and Cognee Cloud.

## Quick start

### Option A: Local mode (default, zero-config server)

The plugin provisions a Python venv, installs cognee, and starts a uvicorn server automatically. The only thing you need to provide is an LLM API key for the Cognee server's graph sync pipeline.

```bash
# 1. Hatch a Vellum assistant
vellum hatch --name my-assistant --remote docker -d

# 2. Store the LLM API key the Cognee server will use for graph sync
vellum exec my-assistant -- assistant credentials set sk-... --service cognee --field llm_api_key

# 3. Install the plugin (triggers the init hook, which provisions and starts the server)
vellum exec my-assistant -- assistant plugins install cognee

# 4. Start a conversation
vellum message my-assistant "hello"
```

### Option B: External / Cognee Cloud server

```bash
# 1. Hatch a Vellum assistant
vellum hatch --name my-assistant --remote docker -d

# 2. Store the Cognee API key, LLM API key, and server base URL
vellum exec my-assistant -- assistant credentials set your-cognee-api-key --service cognee --field api_key
vellum exec my-assistant -- assistant credentials set sk-... --service cognee --field llm_api_key
vellum exec my-assistant -- assistant credentials set https://your-cognee-server-url --service cognee --field base_url

# 3. Install the plugin (auto-detects cloud mode from the base_url credential)
vellum exec my-assistant -- assistant plugins install cognee

# 4. Start a conversation
vellum message my-assistant "hello"
```

## Architecture

This is a **TypeScript plugin** that runs under Bun. In local mode, the init hook provisions a Python venv, installs cognee, and spawns a uvicorn server as a subprocess. All HTTP calls to the Cognee API use Bun's native `fetch`.

### File layout

```
vellum-assistant/
  package.json              # Vellum plugin manifest
  src/                      # Core logic (client, config, server management, session bridge)
  hooks/                    # Plugin lifecycle hooks (init, user-prompt-submit, post-tool-use, stop, etc.)
  tools/                    # Model-visible tools (cognee-recall)
  skills/                   # User-facing skills (cognee-remember, cognee-search, cognee-sync)
```

### Disabling Vellum's default memory

The `init` hook disables Vellum's built-in memory system so Cognee is the sole memory provider:

1. **Config flags**: Writes `memory.enabled = false` and `memory.v2.enabled = false` to `<workspace>/config.json`. The daemon's config cache auto-invalidates on file change.

2. **Default plugin sentinels**: Creates `.disabled` sentinel files at:
   - `<workspace>/plugins/default-memory-retrieval/.disabled`
   - `<workspace>/plugins/default-memory-v3-shadow/.disabled`

This works because user plugin `init` hooks run **before** `bootstrapPlugins()` checks the `.disabled` sentinels for default plugins.

> **Note:** Vellum intends to make this easier in the future with a first-class plugin API for opting out of the default memory system. The current approach is a workaround until that ships.

### Circuit breaker

Recall calls go through a file-based circuit breaker (`$VELLUM_WORKSPACE_DIR/plugins/cognee/data/recall-breaker.json`). After 5 consecutive failures (UNREACHABLE or 5xx), the breaker opens for 120 seconds. A reachable 4xx (auth error) does NOT trip the breaker — waiting won't fix a config problem.

### Session management

The host session key (Vellum `conversationId`) maps to a deterministic Cognee session ID via first-writer-wins file creation at `$VELLUM_WORKSPACE_DIR/plugins/cognee/data/vellum-assistant/sessions/<hostKey>.json`. A separate per-launch `conn_uuid` is the registration/liveness handle.

### Plugin directory

The plugin is installed at `$VELLUM_WORKSPACE_DIR/plugins/cognee/`. All state lives under `$VELLUM_WORKSPACE_DIR/plugins/cognee/data/` (shared: API key cache, server-ready marker, circuit breaker) and `$VELLUM_WORKSPACE_DIR/plugins/cognee/data/vellum-assistant/` (per-session: logs, session maps, bridge cache).

## Configuration

### Config file

`$VELLUM_WORKSPACE_DIR/plugins/cognee/config.json` — standard plugin config location, read by the host on init:

```json
{
  "mode": "local",
  "base_url": "http://127.0.0.1:8011",
  "base_url_credential": "cognee:base_url",
  "api_key_credential": "cognee:api_key",
  "llm_api_key_credential": "cognee:llm_api_key",
  "dataset": "agent_sessions",
  "agent_name": "vellum-assistant",
  "session_prefix": "vellum",
  "auto_improve_every": 30
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `mode` | `local` | `local` = plugin manages server (venv + uvicorn), `cloud`/`server` = external |
| `base_url` | `http://127.0.0.1:8011` | Cognee server URL |
| `base_url_credential` | `cognee:base_url` | Credential reference `service:field` for the Cognee server URL (enables zero-config Option B) |
| `api_key_credential` | `cognee:api_key` | Credential reference `service:field` for the Cognee API key |
| `llm_api_key_credential` | `cognee:llm_api_key` | Credential reference `service:field` for the LLM key (graph sync) |
| `dataset` | `agent_sessions` | Dataset name for storage |
| `agent_name` | `vellum-assistant` | Agent name for session IDs |
| `session_prefix` | `vellum` | Session ID prefix |
| `auto_improve_every` | `30` | Save count before auto-sync to graph |

### Credential store integration

The plugin resolves credentials via `assistant credentials reveal --service <s> --field <f> --json` at runtime. Three credential references are supported:

- **`base_url_credential`** (e.g. `cognee:base_url`) — the Cognee server URL. When set, the plugin auto-detects cloud/server mode from the URL and skips the local server setup. This enables the zero-config Option B flow.
- **`api_key_credential`** (e.g. `cognee:api_key`) — authenticates the plugin to the Cognee server. For local servers, can be left empty (auto-minted on first run).
- **`llm_api_key_credential`** (e.g. `cognee:llm_api_key`) — the LLM key the Cognee server needs for its cognify pipeline (graph sync). In local mode, the plugin passes this to the spawned server as `COGNEE_LLM_API_KEY`. For remote servers, configure the LLM key on the server itself.

## Cognee server

In local mode, the plugin manages the Cognee server lifecycle automatically — it provisions a Python venv, installs cognee, and starts a uvicorn server at the configured `base_url` (default `http://127.0.0.1:8011`). The init hook is triggered on plugin install.

In cloud/server mode, the Cognee server must already be running at the configured `base_url`. If the server is unreachable, all hooks degrade gracefully (no-ops) and the circuit breaker prevents hammering.

### LLM API key (required for graph sync)

The `/api/v1/remember` endpoint (used for session-to-graph sync) runs Cognee's cognify pipeline, which requires an LLM API key on the server. Without it, graph sync will fail with `LLMAPIKeyNotSetError`.

Session memory (`/api/v1/remember/entry` for QA pairs and traces) does **not** require an LLM key and works without one.

**In local mode**: the plugin resolves `llm_api_key_credential` via the credential store and passes it to the spawned server as `COGNEE_LLM_API_KEY`. Set it via `assistant credentials set --service cognee --field llm_api_key`.

**In cloud/server mode**: configure the LLM key on the Cognee server itself:

```bash
curl -X POST http://localhost:8011/api/v1/settings \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: <key>" \
  -d '{"llm_api_key":"sk-..."}'
```

The init hook checks for an LLM key and logs a warning if none is configured.

## API key resolution

1. Credential store (via `assistant credentials reveal` if `api_key_credential` is set)
2. `COGNEE_API_KEY` env var (manual override)
3. Cached key at `$VELLUM_WORKSPACE_DIR/plugins/cognee/data/api_key.json` (auto-minted on first init for local servers)
4. For local servers with no key: the init hook mints one via `/api/v1/auth/login` + `/api/v1/auth/api-keys`

## Development

The quick start uses `assistant plugins install cognee`, which pulls from the Vellum marketplace (pinned to a specific commit of the upstream repo). To install from a fork or branch instead, use the full GitHub URL form:

```bash
assistant plugins install https://github.com/vellum-ai/cognee-integrations/tree/<branch>/integrations/vellum-assistant --name cognee
```
