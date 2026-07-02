# Environment Variable Reference

Unified reference for every environment variable consumed by the in-scope Cognee
integrations. Variables are grouped by function. The **Consumed by** column shows
which integrations read the variable: **CC** (Claude Code), **CX** (Codex),
**HA** (Hermes Agent), **OC** (OpenClaw), **n8n** (n8n).

> n8n configures connection settings through n8n credentials (Base URL + API Key),
> not environment variables. Its entries below are marked N/A.

## Connection & Mode

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_BASE_URL` | _(unset)_ | Cognee API base URL. When set, activates **remote / cloud** mode. When unset, the plugin uses local-server mode. | CC · CX · HA · OC |
| `COGNEE_API_KEY` | _(unset; auto-minted in local mode)_ | API key for authenticating with the Cognee server. In local mode, auto-minted and cached to `~/.cognee-plugin/api_key.json`. | CC · CX · HA · OC |
| `COGNEE_SERVICE_URL` | _(unset)_ | Deprecated alias for `COGNEE_BASE_URL`. Lower precedence; use `COGNEE_BASE_URL` for new setups. | HA |
| `COGNEE_LOCAL_API_URL` | `http://localhost:8011` | Override the local Cognee API endpoint (local-server mode only). | CC · CX |
| `COGNEE_LOCAL_PORT` | `8000` | Port for the local Cognee server. | HA |
| `COGNEE_EMBEDDED` | `false` | Set to `true` to run Cognee in-process (embedded mode). Safe for single-process / offline use only. | HA |
| `COGNEE_MODE` | `local` | Runtime mode: `"local"` or `"cloud"`. | OC |
| `LLM_API_KEY` | _(unset)_ | API key for the LLM provider (e.g. OpenAI). Required for local mode. | CC · CX · HA |
| `LLM_MODEL` | _(unset)_ | LLM model name (e.g. `gpt-4o-mini`). | CC · CX · HA |

## Identity & Auth

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_USER_EMAIL` | `default_user@example.com` | Email for the local Cognee user identity. | CC · CX |
| `COGNEE_USER_PASSWORD` | `default_password` | Password for the local Cognee user identity. | CC · CX |
| `COGNEE_HERMES_USER_EMAIL` | `default_user@example.com` | Email for the Hermes-specific Cognee user. | HA |
| `COGNEE_HERMES_USER_PASSWORD` | `default_password` | Password for the Hermes-specific Cognee user. | HA |
| `COGNEE_USERNAME` | _(empty)_ | Username for Cognee authentication (local-mode login). | OC |
| `COGNEE_PASSWORD` | _(empty)_ | Password for Cognee authentication (local-mode login). | OC |
| `COGNEE_USER_ID` | _(empty)_ | Cognee user ID, set at runtime after authentication. | CC · CX |
| `OPENCLAW_USER_ID` | _(empty)_ | User identifier for user-scoped memory in OpenClaw. | OC |
| `OPENCLAW_AGENT_ID` | `default` | Agent identifier for agent-scoped memory in OpenClaw. | OC |

## Dataset & Session

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_PLUGIN_DATASET` | `agent_sessions` | Dataset name for writes and recall. | CC · CX |
| `COGNEE_DATASET` | `hermes` | Dataset name for writes and recall. | HA |
| `COGNEE_SESSION_ID` | _(auto-generated per launch)_ | Override to resume a specific named session. | CC · CX |
| `COGNEE_SESSION_STRATEGY` | `per-directory` | Session ID strategy: `per-directory`, `git-branch`, or `static`. | CC · CX |
| `COGNEE_SESSION_PREFIX` | `claude` / `codex` / `hermes` | Prefix for auto-generated session IDs. | CC · CX · HA |
| `COGNEE_SESSION_KEY` | _(set by host at runtime)_ | Host session key used for internal session correlation. Not user-facing. | CC · CX |
| `COGNEE_AGENT_NAME` | `claude-code-agent` | Agent name for session registration. | CC · CX |
| `COGNEE_AGENT_SESSION_NAME` | _(empty)_ | Session name passed to sync-session-to-graph. Internal. | CC · CX |

## Server Bootstrap

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_SERVER_BOOT_DEADLINE` | `600` | Maximum seconds to wait for the local Cognee server to start. | CC · CX |
| `COGNEE_SERVER_BOOT_TIMEOUT` | `30` | Maximum seconds to wait for the local Cognee server to start. | HA |
| `COGNEE_LAZY_BOOTSTRAP` | `1` (enabled) | When `1`, defers venv creation and server boot until first use. Set to `0` to bootstrap eagerly at SessionStart. | CC · CX |
| `COGNEE_PLUGIN_PYTHON` | `3.12` | Python version to use when creating the plugin's virtualenv. | CC · CX |
| `COGNEE_INSTALL_TIMEOUT` | `600` | Maximum seconds for `pip install cognee` in the plugin venv. | CC · CX |

## Recall & Search

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_RECALL_TIMEOUT` | `2.5` (context lookup) / `20` (client) | HTTP timeout in seconds for recall requests. Context lookup uses a tighter default. | CC · CX |
| `COGNEE_RECALL_BUDGET` | `4.0` | Overall time budget in seconds for the recall pipeline (probe + fetch + format). | CC · CX |
| `COGNEE_READY_PROBE_TIMEOUT` | `1.0` | Timeout for the health-check probe before attempting recall. | CC · CX |
| `COGNEE_TOP_K` | `5` | Maximum number of results to return from recall. | HA |

## Remember & Cognify

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_REMEMBER_BACKGROUND` | `true` | When `true`, the graph build runs in the background after a remember call. Set `false` for synchronous, immediately-queryable writes. | CC · CX |
| `COGNEE_REMEMBER_WAIT_SECONDS` | `8.0` | For explicit "remember this" calls: bounded wait time in seconds. `0` disables the wait entirely. | CC · CX |
| `COGNEE_COGNIFY_POLL_INTERVAL` | `3.0` | Seconds between status polls while waiting for a background cognify to complete. | CC · CX |
| `COGNEE_STATUS_REQUEST_TIMEOUT` | `10.0` | HTTP timeout for each individual cognify status poll request. | CC · CX |
| `COGNEE_AUTO_IMPROVE_EVERY` | _(empty; disabled)_ | If set to a positive integer, auto-triggers an improve cycle every N writes. | CC · CX |

## Session Sync & Graph Bridge

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_BRIDGE_POLL_DEADLINE` | `600.0` | Overall deadline in seconds for session→graph bridge polling. | CC · CX |
| `COGNEE_BRIDGE_SUBMIT_TIMEOUT` | `30.0` | HTTP read timeout for the background bridge POST (enqueue is fast). | CC · CX |
| `COGNEE_SYNC_START_DELAY` | _(empty)_ | Delay in seconds before the detached final sync starts. | CC · CX |
| `COGNEE_SYNC_RETRIES` | `3` | Number of retry attempts for the detached final sync. | CC · CX |
| `COGNEE_SYNC_RETRY_DELAY` | `5` | Seconds between retries for the detached final sync. | CC · CX |
| `COGNEE_SYNC_SESSION_ID` | _(empty)_ | Session ID override for the sync worker. Internal. | CC · CX |
| `COGNEE_SYNC_DATASET` | _(empty)_ | Dataset override for the sync worker. Internal. | CC · CX |
| `COGNEE_UNREGISTER_ON_FINISH` | _(empty)_ | If `true`, unregister the agent after detached final sync completes. | CC · CX |
| `COGNEE_IMPROVE_ON_END` | `true` | Run `improve()` at session end to bridge session memory into the graph. | HA |
| `COGNEE_IMPROVE_BACKGROUND` | _(auto)_ | Whether `improve()` runs in the background. Auto: backgrounds in server/remote mode, synchronous in embedded mode. Set `true` or `false` to force. | HA |
| `COGNEE_IMPROVE_TIMEOUT` | `300` | HTTP timeout in seconds for the improve call. | HA |
| `COGNEE_WRITE_TIMEOUT` | `120` | HTTP timeout in seconds for write/remember calls. | HA |

## Idle Watcher

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_IDLE_POLL` | `10` | Poll interval in seconds for the idle watcher. | CC · CX |
| `COGNEE_IDLE_THRESHOLD` | `60` | Seconds of inactivity before idle sync fires. | CC · CX |
| `COGNEE_IMPROVE_COOLDOWN` | `120` | Minimum seconds between idle sync runs. | CC · CX |
| `COGNEE_IDLE_DISABLED` | `false` | Set to `1` / `true` / `yes` to disable the idle watcher entirely. | CC · CX |

## Circuit Breaker

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_BREAKER_THRESHOLD` | `5` | Number of consecutive failures before the circuit breaker opens. | CC · CX |
| `COGNEE_BREAKER_COOLDOWN` | `120` | Seconds to wait before retrying after the circuit breaker opens. | CC · CX |

## Memory Steering (Claude Code only)

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_PREFER_MEMORY` | `true` | Inject a SessionStart instruction asserting Cognee as the preferred memory system over Claude Code's native auto memory (`MEMORY.md`). | CC |
| `COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE` | _(disabled)_ | Clear the transcript file on `Stop` after memory capture. For demo flows. | CC |

## Backend Selection

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_CLAUDE_BACKEND` | `auto` | Force the Claude Code plugin's backend mode: `native`/`local`/`sdk` (in-process), `http`/`api`/`cloud`/`server` (remote), or `auto` (infer from `COGNEE_BASE_URL`). | CC |
| `COGNEE_CODEX_BACKEND` | `auto` | Same as above, for the Codex plugin. | CX |
| `COGNEE_AGENT_MODE` | _(empty)_ | Internal mode flag set during runtime. | CC · CX · HA |

## Storage Paths

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_HOME` | `~/.cognee-plugin` | Root directory for all plugin state and config. | CC · CX |
| `COGNEE_PLUGIN_STATE_DIR` | `~/.cognee-plugin/<integration>/` | Integration-specific state directory. | CC · CX |
| `COGNEE_CACHE_DIR` | _(derived from HOME)_ | Cache directory for the plugin. | CC · CX |
| `COGNEE_DATA_DIR` | _(derived from HOME)_ | Data directory for Cognee local stores. | CC · CX |
| `COGNEE_SYSTEM_DIR` | _(derived from HOME)_ | System directory for Cognee local stores. | CC · CX |
| `COGNEE_DATA_ROOT` | `$HERMES_HOME/cognee/data` | Data root for Cognee stores in Hermes. | HA |
| `COGNEE_SYSTEM_ROOT` | `$HERMES_HOME/cognee/system` | System root for Cognee stores in Hermes. | HA |

## Miscellaneous

| Variable | Default | Effect | Consumed by |
|---|---|---|---|
| `COGNEE_PLUGIN_VERBOSE` | `false` | Enable verbose plugin logging. Set to `1` / `true` / `yes`. | CC · CX |
| `COGNEE_PLUGIN_IN_VENV` | _(empty)_ | Internal flag indicating the hook is running inside the plugin's own virtualenv. | CC · CX |
| `COGNEE_VERSION` | _(empty)_ | Pin the `cognee` package version for installation. | CC · CX |
| `COGNEE_AUTO_ROUTE` | `true` | Enable auto-routing of memory writes to the appropriate scope. | HA |
| `COGNEE_RECALL_TIMEOUT` | `60` | HTTP timeout for recall requests (Hermes — longer default than CC/CX due to no circuit breaker). | HA |
