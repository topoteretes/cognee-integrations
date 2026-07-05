# Environment variable reference

Every environment variable consumed by the Cognee integrations, with its default and effect.

**Precedence** (highest wins): environment variable → config file (`~/.cognee-plugin/config.json`, `HERMES_HOME/cognee.json`, or the plugin's config JSON) → built-in default. An empty value is treated as unset.

**Applies-to** column: `cc` = claude-code, `codex` = codex, `hermes` = hermes-agent, `openclaw` = openclaw. (The n8n node is configured through n8n credentials, not environment variables.)

> Values are compiled from each integration's `config` module and call sites. Where a variable's default differs between integrations or read sites, both are listed.

---

## Connection & mode

| Variable | Default | Effect | Applies to |
|---|---|---|---|
| `COGNEE_BASE_URL` | *(empty → local)* | Cognee service URL. Setting it selects server/cloud mode; empty runs the embedded/local stack. The URL is the sole router — a URL alone is a complete instruction. | cc, codex, hermes, openclaw |
| `COGNEE_SERVICE_URL` | *(empty)* | **Deprecated** alias for `COGNEE_BASE_URL`, kept for backward compatibility. | hermes |
| `COGNEE_API_KEY` | *(empty)* | API key sent as `X-Api-Key` for server/cloud mode. Ignored (dropped) when no base URL is set. | cc, codex, hermes, openclaw |
| `COGNEE_MODE` | `local` | openclaw connection mode: `cloud` or `local`. | openclaw |
| `COGNEE_CLAUDE_BACKEND` | `auto` | Backend selector: `auto` \| `native`/`local`/`sdk` (in-process) \| `http`/`api`/`cloud`/`server` (remote). | cc |
| `COGNEE_CODEX_BACKEND` | `auto` | Same as above, for codex. | codex |
| `COGNEE_EMBEDDED` | `false` | Run cognee fully in-process (single-process/offline only). Otherwise local mode boots a local server on `COGNEE_LOCAL_PORT` for DB safety. | hermes |
| `COGNEE_LOCAL_PORT` | `8000` | Port for the local server in local (non-embedded) mode. | hermes |
| `COGNEE_LOCAL_API_URL` | *(empty)* | Explicit local API URL override for the local server. | cc, codex |
| `LLM_API_KEY` | *(empty)* | LLM provider key used by the local cognify pipeline (required for local mode). | cc, codex, hermes |
| `LLM_MODEL` | *(empty)* | LLM model override for local mode. | cc, codex, hermes |

## Authentication & identity

| Variable | Default | Effect | Applies to |
|---|---|---|---|
| `COGNEE_USER_EMAIL` | `default_user@example.com` | Default principal email (local single-principal model). | cc, codex |
| `COGNEE_USER_PASSWORD` | `default_password` | Default principal password. | cc, codex |
| `COGNEE_HERMES_USER_EMAIL` | `hermes-agent@cognee.local` | Hermes identity email. | hermes |
| `COGNEE_HERMES_USER_PASSWORD` | `hermes-agent-plugin` | Hermes identity password. | hermes |
| `COGNEE_USERNAME` | *(empty)* | openclaw username (server/cloud auth). | openclaw |
| `COGNEE_PASSWORD` | *(empty)* | openclaw password (server/cloud auth). | openclaw |
| `OPENCLAW_USER_ID` | *(empty)* | openclaw user-scope identifier for multi-scope memory. | openclaw |
| `OPENCLAW_AGENT_ID` | `default` | openclaw agent-scope identifier. | openclaw |

## Datasets, sessions & scope

| Variable | Default | Effect | Applies to |
|---|---|---|---|
| `COGNEE_PLUGIN_DATASET` | `agent_sessions` | Dataset name the plugin reads/writes. | cc, codex |
| `COGNEE_DATASET` | `hermes` | Dataset name. | hermes |
| `COGNEE_AGENT_NAME` | `claude-code-agent` / `codex-agent` | Agent name; session id is `{agent}_{host_session_id}`. | cc, codex |
| `COGNEE_SESSION_STRATEGY` | `per-directory` | How the session id is derived: `per-directory` \| `git-branch` \| `static`. | cc, codex |
| `COGNEE_SESSION_PREFIX` | `claude` / `codex` / `hermes` | Prefix for the generated session id. | cc, codex, hermes |
| `COGNEE_SESSION_ID` | *(empty)* | Explicit static session id override (legacy compat). | cc, codex |
| `COGNEE_TOP_K` | `3` (cc/codex) · `5` (hermes) | Number of recall results returned. | cc, codex, hermes |
| `COGNEE_SYNC_DATASET` | *(empty)* | Dataset override for the detached session→graph sync. | cc, codex |
| `COGNEE_AGENT_SESSION_NAME` | *(empty)* | Agent session-name override used by the sync path. | cc, codex |

## Recall tuning & resilience

| Variable | Default | Effect | Applies to |
|---|---|---|---|
| `COGNEE_RECALL_TIMEOUT` | `20` (recall client) · `2.5` (context-lookup hook) · `60` (hermes) | Per-request recall timeout, in seconds. | cc, codex, hermes |
| `COGNEE_RECALL_BUDGET` | `4.0` | Overall wall-clock budget (s) for a recall attempt across retries. | cc, codex |
| `COGNEE_READY_PROBE_TIMEOUT` | `1.0` | Timeout (s) for the server health/ready probe. | cc, codex |
| `COGNEE_BREAKER_THRESHOLD` | `5` | Consecutive backend failures before the recall circuit breaker opens. | cc, codex |
| `COGNEE_BREAKER_COOLDOWN` | `120` | Seconds the breaker stays open (recall short-circuits) before retrying. | cc, codex |

## Write / remember / cognify / bridge

| Variable | Default | Effect | Applies to |
|---|---|---|---|
| `COGNEE_REMEMBER_WAIT_SECONDS` | `8.0` | Bounded wait for an explicit "remember this"; `0` disables the wait. | cc, codex |
| `COGNEE_REMEMBER_BACKGROUND` | *(empty → auto)* | Force remember to run in background (`1`/`true`) or foreground. | cc, codex |
| `COGNEE_BRIDGE_SUBMIT_TIMEOUT` | `30.0` | Read timeout (s) for the background remember POST (enqueue is fast). | cc, codex |
| `COGNEE_BRIDGE_POLL_DEADLINE` | `600.0` | Overall wait (s) for the session→graph bridge to reach COMPLETED. | cc, codex |
| `COGNEE_COGNIFY_POLL_INTERVAL` | `3.0` | Seconds between cognify status polls. | cc, codex |
| `COGNEE_STATUS_REQUEST_TIMEOUT` | `10.0` | Per-poll status GET timeout (s). | cc, codex |
| `COGNEE_WRITE_TIMEOUT` | `120` | Write (remember) timeout, in seconds. | hermes |

## Improve & idle watcher

| Variable | Default | Effect | Applies to |
|---|---|---|---|
| `COGNEE_IMPROVE_ON_END` | `true` | Run `improve` when the session ends. | hermes |
| `COGNEE_IMPROVE_BACKGROUND` | *(empty → auto)* | Force improve to run background/foreground (auto = background in server/remote mode). | hermes |
| `COGNEE_IMPROVE_TIMEOUT` | `300` | Improve timeout, in seconds. | hermes |
| `COGNEE_AUTO_ROUTE` | `true` | Auto-route memory operations. | hermes |
| `COGNEE_AUTO_IMPROVE_EVERY` | *(empty → off)* | Trigger an improve every N recorded interactions. | cc, codex |
| `COGNEE_IMPROVE_COOLDOWN` | `120` | Minimum seconds between auto-improves. | cc, codex |
| `COGNEE_IDLE_DISABLED` | *(empty → enabled)* | Set truthy to disable the idle watcher. | cc, codex |
| `COGNEE_IDLE_POLL` | `10` | Idle-watcher poll interval, in seconds. | cc, codex |
| `COGNEE_IDLE_THRESHOLD` | `60` | Idle seconds before the watcher flushes/persists. | cc, codex |
| `COGNEE_PREFER_MEMORY` | `true` | Assert Cognee as the preferred memory over the host's built-in memory (e.g. `MEMORY.md`). | cc |
| `COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE` | *(empty)* | Clear the injected recall context after a message. | cc |

## Detached sync tuning

| Variable | Default | Effect | Applies to |
|---|---|---|---|
| `COGNEE_SYNC_START_DELAY` | *(empty → 0)* | Delay (s) before the detached session→graph sync starts. | cc, codex |
| `COGNEE_SYNC_RETRIES` | *(internal default)* | Retry attempts for the detached sync. | cc, codex |
| `COGNEE_SYNC_RETRY_DELAY` | *(internal default)* | Delay (s) between detached-sync retries. | cc, codex |

## Bootstrap, paths & install

| Variable | Default | Effect | Applies to |
|---|---|---|---|
| `COGNEE_INSTALL_TIMEOUT` | `600.0` | Timeout (s) for the one-time `cognee` install into the plugin venv. | cc, codex |
| `COGNEE_SERVER_BOOT_TIMEOUT` | `30` | Local-server boot timeout, in seconds. | hermes |
| `COGNEE_SERVER_BOOT_DEADLINE` | `600.0` | Local-server boot deadline (s). | cc, codex |
| `COGNEE_LAZY_BOOTSTRAP` | `1` | Lazy plugin bootstrap; set falsy to bootstrap eagerly at session start. | cc, codex |
| `COGNEE_PLUGIN_PYTHON` | *(empty → `3.12`)* | Python interpreter pinned for the plugin venv. | cc, codex |
| `COGNEE_PLUGIN_STATE_DIR` | `~/.cognee-plugin` | Directory for plugin state (breaker, bridge, logs). | cc, codex |
| `COGNEE_PLUGIN_VERBOSE` | *(empty)* | Set truthy for verbose plugin logging. | cc, codex |
| `COGNEE_DATA_ROOT` / `COGNEE_DATA_DIR` | *(empty)* | Cognee data root (`DATA_ROOT_DIRECTORY`). | hermes / cc, codex |
| `COGNEE_SYSTEM_ROOT` / `COGNEE_SYSTEM_DIR` | *(empty)* | Cognee system root (`SYSTEM_ROOT_DIRECTORY`). | hermes / cc, codex |
| `COGNEE_CACHE_DIR` | *(empty)* | Cognee cache root (`CACHE_ROOT_DIRECTORY`). | cc, codex |

## Plugin-managed / internal

These are set by the plugin itself or used for testing — not intended as user configuration.

| Variable | Effect | Applies to |
|---|---|---|
| `COGNEE_SESSION_KEY` | In-process correlation key for the host (Claude/Codex) session id; set by hooks before resolving the Cognee session id. | cc, codex |
| `COGNEE_SYNC_SESSION_ID` | Session id handed to the detached sync subprocess. | cc, codex |
| `COGNEE_UNREGISTER_ON_FINISH` | Set by the plugin to request session unregister on finish. | cc, codex |
| `COGNEE_PLUGIN_IN_VENV` | Internal marker that the plugin is already running inside its venv. | cc, codex |
| `COGNEE_AGENT_MODE` | Internal agent-mode marker (registration path; not selected via this env). | cc, codex |
| `COGNEE_RUN_INTEGRATION` | Gate that enables the live integration tests. | cc, codex, hermes |
| `CLAUDE_CWD` / `CODEX_CWD` | Host-provided working directory used for `per-directory` session strategy. | cc / codex |
