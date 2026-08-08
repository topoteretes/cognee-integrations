# Environment Variable Reference — cognee-integrations

> **Scan coverage**
> - `integrations/claude-code/` — hooks, scripts, all Python files
> - `integrations/codex/` — plugins/cognee/scripts, all Python files
> - `integrations/openclaw/` — src/*.ts, docker-compose
> - `integrations/hermes-agent/` — cognee_integration_hermes/*.py
> - `integrations/n8n/` — credentials, nodes
> - `n8n_workflows/cognee_skill_self_improve/` — advanced runner
> - `E:/cognee/cognee/.env.template` — upstream cognee core variables inherited when integrations run the local SDK or server
>
> **Key to the "Integration" column**
> `cc` = claude-code · `cx` = codex · `oc` = openclaw · `ha` = hermes-agent · `n8n` = n8n-node · `wf` = n8n advanced workflow runner

---

## 1. Core Connection

How the integration reaches Cognee (cloud endpoint vs. local server vs. embedded SDK).

| Variable | Integration | Default | Description |
|---|---|---|---|
| `COGNEE_BASE_URL` | `cc` `cx` `oc` `ha` | _(unset)_ | URL of a managed/cloud Cognee endpoint. When set the plugin skips local server bootstrap and connects as a thin HTTP client. Canonical name; `COGNEE_SERVICE_URL` is a deprecated alias (ha only). |
| `COGNEE_SERVICE_URL` | `ha` | _(unset)_ | **Deprecated** alias for `COGNEE_BASE_URL`. Lower precedence; new setups should use `COGNEE_BASE_URL`. |
| `COGNEE_API_KEY` | `cc` `cx` `oc` `ha` | _(unset)_ | API key sent as `X-Api-Key`. Auto-minted from the default local user in local mode; cached to `~/.cognee-plugin/api_key.json`. |
| `COGNEE_LOCAL_API_URL` | `cc` `cx` | `http://localhost:8011` | Override the URL the plugin boots / talks to in local mode. |
| `COGNEE_MODE` | `oc` | `local` | Set to `cloud` to activate cloud mode (alternative to `COGNEE_BASE_URL` for openclaw). |
| `COGNEE_USERNAME` | `oc` | _(unset)_ | Auth username for openclaw local mode. |
| `COGNEE_PASSWORD` | `oc` | _(unset)_ | Auth password for openclaw local mode. |
| `COGNEE_EMBEDDED` | `ha` | `false` | Run cognee in-process (embedded SDK) instead of talking to a server. **Single-process / offline only** — risks DB lock errors with concurrent writers. |
| `COGNEE_LOCAL_PORT` | `ha` | `8000` | Port for the local cognee server managed by hermes-agent. |
| `COGNEE_SERVER_BOOT_TIMEOUT` | `ha` | `30` | Seconds to wait for the local server to become healthy (hermes-agent). |
| `COGNEE_USER_EMAIL` | `cx` | `default_user@example.com` | Default-user email for the codex plugin's local-server auth flow. |
| `COGNEE_USER_PASSWORD` | `cx` | `default_password` | Default-user password for the codex plugin's local-server auth flow. |
| `COGNEE_HERMES_USER_EMAIL` | `ha` | `hermes-agent@cognee.local` | Identity email used when hermes-agent provisions or authenticates a local user. |
| `COGNEE_HERMES_USER_PASSWORD` | `ha` | `hermes-agent-plugin` | Identity password to match `COGNEE_HERMES_USER_EMAIL`. |

---

## 2. LLM / Model (local mode)

Required only when running a **local** Cognee server or SDK (no `COGNEE_BASE_URL`).

| Variable | Integration | Default | Description |
|---|---|---|---|
| `LLM_API_KEY` | `cc` `cx` `ha` `wf` + core | _(unset)_ | OpenAI / LiteLLM API key. The one required variable for local mode. Also used as the default embedding key if `EMBEDDING_API_KEY` is not set. |
| `LLM_MODEL` | `cc` `cx` `ha` + core | _(unset)_ | Model name (e.g. `openai/gpt-4o-mini`). Falls back to cognee core defaults when unset. |
| `LLM_PROVIDER` | cognee core | `openai` | LiteLLM provider string (e.g. `openai`, `azure`, `ollama`, `custom`). |
| `LLM_ENDPOINT` | cognee core | _(unset)_ | Custom LLM API endpoint (Azure, Ollama, OpenRouter, etc.). |
| `LLM_API_VERSION` | cognee core | _(unset)_ | API version string (required for Azure OpenAI). |
| `LLM_MAX_COMPLETION_TOKENS` | cognee core | `16384` | Max tokens for completion calls (used for chunk size calculations; not forwarded in LLM calls). |
| `LLM_TEMPERATURE` | cognee core | `0.0` | Sampling temperature. |
| `LLM_STREAMING` | cognee core | `false` | Enable streaming LLM responses. |
| `LLM_ARGS` | cognee core | `{}` | JSON string of extra kwargs passed to every LLM completion call. |
| `LLM_RATE_LIMIT_ENABLED` | cognee core | `true` | Enable per-process LLM rate limiting. |
| `LLM_RATE_LIMIT_REQUESTS` | cognee core | `60` | Max LLM requests per interval. |
| `LLM_RATE_LIMIT_INTERVAL` | cognee core | `60` | Rate-limit interval in seconds. |
| `LLM_RATE_LIMIT_TOKENS` | cognee core | `0` | Token-based rate limit (0 = disabled). |
| `LLM_AZURE_USE_MANAGED_IDENTITY` | cognee core | _(unset)_ | Use Azure `DefaultAzureCredential` — no API key needed. |
| `LLM_INSTRUCTOR_MODE` | cognee core | _(unset)_ | Instructor extraction mode (e.g. `json_schema_mode`). |
| `FALLBACK_MODEL` | cognee core | _(unset)_ | Fallback LLM when primary completion fails. |
| `FALLBACK_API_KEY` | cognee core | _(unset)_ | API key for `FALLBACK_MODEL`. |
| `FALLBACK_ENDPOINT` | cognee core | _(unset)_ | Endpoint for `FALLBACK_MODEL`. |
| `TRANSCRIPTION_MODEL` | cognee core | `whisper-1` | Audio transcription model. |
| `STRUCTURED_OUTPUT_FRAMEWORK` | cognee core | `instructor` | Structured extraction framework: `instructor` or `baml`. |

---

## 3. Embedding

| Variable | Integration | Default | Description |
|---|---|---|---|
| `EMBEDDING_PROVIDER` | cognee core | `openai` | Embedding provider (e.g. `openai`, `ollama`). |
| `EMBEDDING_MODEL` | cognee core | `openai/text-embedding-3-large` | Embedding model name. |
| `EMBEDDING_DIMENSIONS` | cognee core | `3072` | Vector dimensions for the embedding model. |
| `EMBEDDING_API_KEY` | cognee core | _(uses `LLM_API_KEY`)_ | Separate API key for embeddings. |
| `EMBEDDING_ENDPOINT` | cognee core | _(unset)_ | Custom embedding endpoint. |
| `EMBEDDING_API_VERSION` | cognee core | _(unset)_ | API version for embedding calls (Azure). |
| `EMBEDDING_MAX_COMPLETION_TOKENS` | cognee core | `8191` | Max tokens per embedding request. |
| `EMBEDDING_BATCH_SIZE` | cognee core | `36` | Batch size for embedding calls. |
| `EMBEDDING_RATE_LIMIT_ENABLED` | cognee core | `false` | Enable per-process embedding rate limiting. |
| `EMBEDDING_RATE_LIMIT_REQUESTS` | cognee core | `60` | Max embedding requests per interval. |
| `EMBEDDING_RATE_LIMIT_INTERVAL` | cognee core | `60` | Rate-limit interval in seconds. |
| `EMBEDDING_RATE_LIMIT_TOKENS` | cognee core | `0` | Token-based embedding limit (0 = disabled). |
| `HUGGINGFACE_TOKENIZER` | cognee core | _(unset)_ | HuggingFace tokenizer for token counting (e.g. with Ollama embeddings). |

---

## 4. Dataset & Session

| Variable | Integration | Default | Description |
|---|---|---|---|
| `COGNEE_PLUGIN_DATASET` | `cc` `cx` | `agent_sessions` | Cognee dataset that the plugin reads and writes. Fixed for the lifetime of a launch. cc and cx share the same default, so memory is shared automatically. |
| `COGNEE_DATASET` | `ha` | `hermes` | Dataset name for the hermes-agent plugin. |
| `COGNEE_SESSION_ID` | `cc` `cx` | _(auto-generated)_ | Override to pin a named Cognee session (resume across launches, share between terminals). |
| `COGNEE_SESSION_STRATEGY` | `cc` `cx` | `per-directory` | How session IDs are auto-generated: `per-directory`, `git-branch`, or `static`. |
| `COGNEE_SESSION_PREFIX` | `cc` `cx` `ha` | `cc` / `codex` / `hermes` | Prefix prepended to auto-generated session IDs. |
| `COGNEE_SESSION_KEY` | `cc` `cx` | _(set from hook payload)_ | Internal host-session correlation key. Set by session-start and forwarded to sync workers via env. |
| `COGNEE_AGENT_NAME` | `cc` `cx` | _(auto)_ | Name of the active agent connection in the Cognee registry. |
| `COGNEE_AGENT_SESSION_NAME` | `cc` `cx` | _(auto)_ | Agent session name for registration / unregistration. Set internally by sync scripts. |
| `COGNEE_USER_ID` | `cc` `cx` | _(resolved at runtime)_ | Principal user ID. Set internally after identity resolution; forwarded to watchers. |
| `COGNEE_SYNC_SESSION_ID` | `cc` `cx` | _(unset)_ | Session ID override for detached sync workers. |
| `COGNEE_SYNC_DATASET` | `cc` `cx` | _(unset)_ | Dataset override for detached sync workers. |
| `OPENCLAW_USER_ID` | `oc` | _(unset)_ | User identifier for user-scoped memory datasets. |
| `OPENCLAW_AGENT_ID` | `oc` | `default` | Agent identifier for agent-scoped memory datasets. |
| `CODEX_CWD` | `cx` | `os.getcwd()` | Working directory injected by Codex; used for per-directory session ID resolution. |

---

## 5. Memory Behaviour

| Variable | Integration | Default | Description |
|---|---|---|---|
| `COGNEE_PREFER_MEMORY` | `cc` | `true` | Inject a `SessionStart additionalContext` instruction steering Claude to treat Cognee as the authoritative memory system over `MEMORY.md`. |
| `COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE` | `cc` | _(disabled)_ | Set `true` to clear the Claude transcript file on `Stop` after memory capture (demo / reset flows). |
| `COGNEE_REMEMBER_BACKGROUND` | `cc` `cx` | `true` | Send `run_in_background=true` to `/api/v1/remember` so the write returns immediately without blocking on graph build. Set `false` for a synchronous immediately-queryable write. |
| `COGNEE_AUTO_IMPROVE_EVERY` | `cc` `cx` | _(internal default)_ | Trigger an `improve()` run every N tool calls within a session (positive integer). Controls the per-session turn counter threshold in `_plugin_common.py`. |
| `COGNEE_IMPROVE_ON_END` | `ha` | `true` | Call `cognee.improve()` at session end to bridge session-cache QAs into the permanent graph. |
| `COGNEE_IMPROVE_BACKGROUND` | `ha` | auto | Force `improve()` to run in the background (`true`/`false`). Default auto: background in server/remote mode, synchronous in embedded mode. |
| `AUTO_FEEDBACK` | `cc` `cx` (forwarded) | `true` | Set by `apply_cognee_env()` and forwarded to the local Cognee server. Tells the server to auto-classify follow-up messages as feedback and attach them to the prior QA. |

---

## 6. Idle Watcher & Sync Workers

| Variable | Integration | Default | Description |
|---|---|---|---|
| `COGNEE_IDLE_POLL` | `cc` `cx` | `10` | Idle watcher poll interval in seconds. |
| `COGNEE_IDLE_THRESHOLD` | `cc` `cx` | `60` | Seconds of inactivity before the idle watcher fires a session sync. |
| `COGNEE_IMPROVE_COOLDOWN` | `cc` `cx` | `120` | Minimum seconds between idle sync runs. |
| `COGNEE_SYNC_START_DELAY` | `cc` `cx` | `2.0` | Seconds the detached final-sync worker waits before starting. |
| `COGNEE_SYNC_RETRIES` | `cc` `cx` | `3` | Retry attempts in the detached final-sync worker. |
| `COGNEE_SYNC_RETRY_DELAY` | `cc` `cx` | `10.0` | Seconds between retries in the detached final-sync worker. |
| `COGNEE_UNREGISTER_ON_FINISH` | `cc` `cx` | _(set internally)_ | Set to `1` by `sync-session-to-graph.py` when spawning the detached worker; tells it to call agent unregister after sync. |

---

## 7. HTTP Client / Circuit Breaker

| Variable | Integration | Default | Description |
|---|---|---|---|
| `COGNEE_BREAKER_THRESHOLD` | `cc` `cx` | `5` | Consecutive failures before the circuit breaker opens. |
| `COGNEE_BREAKER_COOLDOWN` | `cc` `cx` | `120` | Seconds the circuit stays open before attempting reset. |
| `COGNEE_RECALL_TIMEOUT` | `cc` `cx` | `20` | HTTP timeout for recall requests (seconds). |
| `COGNEE_RECALL_TIMEOUT` | `ha` | `60` | HTTP timeout for recall requests in hermes-agent (seconds). |
| `COGNEE_WRITE_TIMEOUT` | `ha` | `120` | HTTP timeout for remember/write requests. |
| `COGNEE_IMPROVE_TIMEOUT` | `ha` | `300` | HTTP timeout for `improve()` calls. |
| `COGNEE_TOP_K` | `ha` | `5` | Max recall results per query. |
| `COGNEE_AUTO_ROUTE` | `ha` | `true` | Enable automatic query routing. |

---

## 8. Plugin Bootstrap & Installation

| Variable | Integration | Default | Description |
|---|---|---|---|
| `COGNEE_PLUGIN_PYTHON` | `cc` `cx` | `3.12` | Python version pinned for the shared `uv`-managed virtualenv (`~/.cognee-plugin/venv`). |
| `COGNEE_SERVER_BOOT_DEADLINE` | `cc` `cx` | `600` | Max seconds the plugin waits for the local Cognee API server to boot (including migrations). |
| `COGNEE_LAZY_BOOTSTRAP` | `cc` `cx` | `1` | Defer server boot + registration to a detached worker so `SessionStart` returns within the 15 s hook budget. Set `0`/`false`/`no` to disable. |
| `COGNEE_INSTALL_TIMEOUT` | `cc` `cx` | `600` | Max seconds allowed for the `uv pip install cognee` step inside the shared venv. |
| `COGNEE_PLUGIN_STATE_DIR` | `cc` `cx` | `~/.cognee-plugin` | Override the root directory for plugin state (sessions, logs, pidfiles). |
| `COGNEE_PLUGIN_IN_VENV` | `cc` `cx` | _(set internally)_ | Sentinel (`1`) set after the plugin re-execs itself into the managed venv; prevents infinite re-exec loops. |
| `COGNEE_AGENT_MODE` | `cc` `cx` | _(set internally)_ | Set `true` on the server subprocess environment so it shuts down when all agents disconnect. |
| `COGNEE_CODEX_BACKEND` | `cx` | `auto` | Backend mode: `auto`, `http`/`api`/`cloud`/`server`, or `native`/`local`/`sdk`. When `native`, the plugin forces local SDK mode regardless of base_url. |
| `UV_UNMANAGED_INSTALL` | `cc` `cx` | _(set internally)_ | Drops the `uv` binary into `~/.cognee-plugin/uv` without shell profile edits. |
| `UV_PYTHON_INSTALL_DIR` | `cc` `cx` | `~/.cognee-plugin/python` | Where `uv` places its managed Python builds. |
| `CLAUDE_PLUGIN_ROOT` | `cc` | _(set by Claude Code)_ | Absolute path to the installed plugin directory. Injected by Claude Code; referenced in `hooks.json`. |

---

## 9. Debug & Logging

| Variable | Integration | Default | Description |
|---|---|---|---|
| `COGNEE_PLUGIN_VERBOSE` | `cc` `cx` | _(unset)_ | Set `1`, `true`, or `yes` to enable verbose hook logging with timestamps. |
| `LOG_LEVEL` | cognee core | `INFO` | Console log level for the cognee SDK/server: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `COGNEE_LOG_FILE` | cognee core | `true` | Set `false` to disable file logging (console-only). |
| `COGNEE_LOGS_DIR` | cognee core | `~/.cognee/logs` | Override the cognee log directory. |
| `COGNEE_LOG_MAX_BYTES` | cognee core | `52428800` | Max size per log file before rotation (bytes). |
| `COGNEE_LOG_BACKUP_COUNT` | cognee core | `5` | Number of rotated log files to keep. |
| `COGNEE_LOG_SEARCH_HISTORY` | cognee core | `true` | Set `false` to disable search query/result logging. |
| `LITELLM_LOG` | cognee core | `ERROR` | LiteLLM internal log level. |
| `ENV` | cognee core | `local` | Runtime environment tag (e.g. `local`, `production`). |
| `TOKENIZERS_PARALLELISM` | cognee core | `false` | Disable HuggingFace tokenizer parallelism (avoids deadlocks in forked processes). |
| `TELEMETRY_DISABLED` | cognee core | _(unset)_ | Set `1` to opt out of anonymous usage telemetry. |
| `COGNEE_RUN_INTEGRATION` | `ha` (tests) | _(unset)_ | Set `1` to opt-in to slow integration tests (`test_integration_concurrency.py`). |

---

## 10. Root Directories (Cognee Core Storage)

These propagate to the cognee SDK / server process. In `cc`/`cx`, the plugin pre-populates them via `apply_cognee_env()` so the local server inherits stable, upgrade-safe paths.

| Variable | Integration | Default | Description |
|---|---|---|---|
| `SYSTEM_ROOT_DIRECTORY` | `cc` `cx` (forwarded) `wf` | `~/.cognee/system` | Cognee system files root. Set by `apply_cognee_env()` via `setdefault`; also directly configurable in cognee core. |
| `DATA_ROOT_DIRECTORY` | `cc` `cx` (forwarded) `wf` | `~/.cognee/data` | Cognee data files root (local databases). Set by `apply_cognee_env()`. |
| `CACHE_ROOT_DIRECTORY` | `cc` `cx` (forwarded) | `~/.cognee/cache` | Cognee cache root. Set by `apply_cognee_env()`. |
| `CACHING` | `cc` `cx` (forwarded) | `true` | Enable the cognee caching layer. Set by `apply_cognee_env()`. |
| `COGNEE_DATA_ROOT` | `ha` | `$HERMES_HOME/cognee/data` | Data root override for hermes-agent's managed cognee instance. |
| `COGNEE_SYSTEM_ROOT` | `ha` | `$HERMES_HOME/cognee/system` | System root override for hermes-agent's managed cognee instance. |

---

## 11. Storage Backend (S3 / AWS)

| Variable | Integration | Default | Description |
|---|---|---|---|
| `STORAGE_BACKEND` | cognee core | `local` | Storage backend: `local` or `s3`. |
| `STORAGE_BUCKET_NAME` | cognee core | _(unset)_ | S3 bucket name. |
| `AWS_REGION` | cognee core | _(unset)_ | AWS region (S3, Bedrock). |
| `AWS_ACCESS_KEY_ID` | cognee core | _(unset)_ | AWS access key ID. |
| `AWS_SECRET_ACCESS_KEY` | cognee core | _(unset)_ | AWS secret access key. |
| `AWS_SESSION_TOKEN` | cognee core | _(unset)_ | AWS session token. |
| `AWS_ENDPOINT_URL` | cognee core | _(unset)_ | Custom AWS endpoint (LocalStack, etc.). |
| `AWS_PROFILE_NAME` | cognee core | _(unset)_ | AWS named profile. |
| `AWS_BEDROCK_RUNTIME_ENDPOINT` | cognee core | _(unset)_ | Custom Bedrock runtime endpoint. |

---

## 12. Relational Database

| Variable | Integration | Default | Description |
|---|---|---|---|
| `DB_PROVIDER` | cognee core | `sqlite` | Relational DB backend: `sqlite` or `postgres`. |
| `DB_NAME` | cognee core | `cognee_db` | Database name. |
| `DB_HOST` | cognee core | `127.0.0.1` | Postgres host. |
| `DB_PORT` | cognee core | `5432` | Postgres port. |
| `DB_USERNAME` | cognee core | `cognee` | Postgres username. |
| `DB_PASSWORD` | cognee core | `cognee` | Postgres password. |
| `DATABASE_CONNECT_ARGS` | cognee core | _(unset)_ | JSON string of extra SQLAlchemy connection args (SSL, timeouts). |
| `POOL_ARGS` | cognee core | _(unset)_ | JSON string for connection pool tuning. |
| `DATABASE_MAX_LRU_CACHE_SIZE` | cognee core | `6` | Max engine instances in the LRU cache per unique connection key (also caps subprocess workers). |

---

## 13. Graph Database

| Variable | Integration | Default | Description |
|---|---|---|---|
| `GRAPH_DATABASE_PROVIDER` | cognee core | `kuzu` | Graph DB backend: `kuzu`, `kuzu-remote`, `neo4j`. |
| `GRAPH_DATASET_DATABASE_HANDLER` | cognee core | `kuzu` | Per-dataset graph DB handler (multi-tenant). |
| `GRAPH_DATABASE_URL` | cognee core | _(unset)_ | Connection URL for remote Kuzu or Neo4j. |
| `GRAPH_DATABASE_NAME` | cognee core | `neo4j` | Database name (Neo4j only). |
| `GRAPH_DATABASE_USERNAME` | cognee core | _(unset)_ | Remote graph DB username. |
| `GRAPH_DATABASE_PASSWORD` | cognee core | _(unset)_ | Remote graph DB password. |
| `GRAPH_DATABASE_HOST` | cognee core | _(unset)_ | Remote graph DB host. |
| `GRAPH_DATABASE_PORT` | cognee core | _(unset)_ | Remote graph DB port. |
| `GRAPH_DATABASE_KEY` | cognee core | _(unset)_ | Remote graph DB auth key. |
| `GRAPH_DATABASE_ALLOW_ANONYMOUS` | cognee core | `false` | Allow anonymous graph DB access. |
| `GRAPH_DATABASE_SUBPROCESS_ENABLED` | cognee core | `true` | Run embedded graph engine in a worker subprocess. |
| `KUZU_NUM_THREADS` | cognee core | `0` (auto) | Kuzu thread count. |
| `KUZU_BUFFER_POOL_SIZE` | cognee core | _(unset)_ | Kuzu buffer pool size in bytes. |
| `KUZU_MAX_DB_SIZE` | cognee core | _(unset)_ | Kuzu max database size in bytes. |
| `SHARED_KUZU_LOCK` | cognee core | `false` | Cross-process file lock for embedded Kuzu. |
| `SHARED_LADYBUG_LOCK` | cognee core | `false` | Cross-process file lock for embedded Ladybug. |

---

## 14. Vector Database

| Variable | Integration | Default | Description |
|---|---|---|---|
| `VECTOR_DB_PROVIDER` | cognee core | `lancedb` | Vector DB: `lancedb`, `pgvector`, `qdrant`, `weaviate`, `milvus`, `chromadb`. |
| `VECTOR_DATASET_DATABASE_HANDLER` | cognee core | `lancedb` | Per-dataset vector DB handler (multi-tenant). |
| `VECTOR_DB_URL` | cognee core | _(unset)_ | Connection URL for remote vector backends. |
| `VECTOR_DB_KEY` | cognee core | _(unset)_ | Auth key for vector DB. |
| `VECTOR_DB_HOST` | cognee core | _(unset)_ | Vector DB host. |
| `VECTOR_DB_PORT` | cognee core | `1234` | Vector DB port. |
| `VECTOR_DB_NAME` | cognee core | _(unset)_ | Vector DB name. |
| `VECTOR_DB_USERNAME` | cognee core | _(unset)_ | Vector DB username. |
| `VECTOR_DB_PASSWORD` | cognee core | _(unset)_ | Vector DB password. |
| `VECTOR_DB_SUBPROCESS_ENABLED` | cognee core | `true` | Run vector engine in a worker subprocess. |
| `VECTOR_POOL_ARGS` | cognee core | _(unset)_ | JSON for PGVector per-dataset pool tuning. |

---

## 15. Dataset Queue & Concurrency

| Variable | Integration | Default | Description |
|---|---|---|---|
| `DATASET_QUEUE_ENABLED` | cognee core | `true` | Semaphore-backed queue limiting concurrent dataset processing slots. |
| `DATASET_QUEUE_MAX_CONCURRENT` | cognee core | `DATABASE_MAX_LRU_CACHE_SIZE` | Max simultaneous dataset slots. |

---

## 16. Session Cache Backend

| Variable | Integration | Default | Description |
|---|---|---|---|
| `CACHE_BACKEND` | cognee core | `sqlite` | Session cache: `sqlite`, `postgres`, `redis`, `fs`, `tapes`. |
| `CACHE_DB_URL` | cognee core | _(auto)_ | Explicit SQLAlchemy async URL for sqlite/postgres backends. |
| `CACHE_HOST` | cognee core | `localhost` | Redis host. |
| `CACHE_PORT` | cognee core | `6379` | Redis port. |
| `CACHE_USERNAME` | cognee core | _(unset)_ | Redis username. |
| `CACHE_PASSWORD` | cognee core | _(unset)_ | Redis password. |
| `CACHE_PURGE_INTERVAL_SECONDS` | cognee core | `900` | Min seconds between global TTL purge sweeps. |
| `SESSION_TTL_SECONDS` | cognee core | `604800` | Session lifetime in the cache (7 days). |
| `MAX_SESSION_CONTEXT_CHARS` | cognee core | _(unset)_ | Per-turn session context character cap. |
| `USAGE_LOGGING` | cognee core | `false` | Per-process LLM usage logging into the cache. |
| `USAGE_LOGGING_TTL` | cognee core | `604800` | Usage log TTL in seconds. |

---

## 17. Security & Authentication

| Variable | Integration | Default | Description |
|---|---|---|---|
| `REQUIRE_AUTHENTICATION` | cognee core | `false` | Force API auth on/off. Ignored (stays on) when `ENABLE_BACKEND_ACCESS_CONTROL=true`. |
| `ENABLE_BACKEND_ACCESS_CONTROL` | cognee core | `true` | Multi-tenant mode: per-user/dataset isolated DBs + mandatory auth. `false` = single-user shared DB. |
| `FASTAPI_USERS_JWT_SECRET` | cognee core | `super_secret` | JWT signing secret. Must match across all instances. **Change in production.** |
| `JWT_LIFETIME_SECONDS` | cognee core | `3600` | JWT token validity period. |
| `HASH_API_KEY` | cognee core | `false` | Store API keys as SHA-256 hashes (shown only once at creation). |
| `ACCEPT_LOCAL_FILE_PATH` | cognee core | `true` | Allow ingesting local filesystem files. Set `false` for hosted deployments. |
| `ALLOW_HTTP_REQUESTS` | cognee core | `true` | Allow outbound HTTP requests (web scraper, etc.). |
| `ALLOW_CYPHER_QUERY` | cognee core | `true` | Allow raw Cypher queries via API. |
| `FASTAPI_USERS_VERIFICATION_TOKEN_SECRET` | cognee core | `super_secret` | Email verification token secret. **Change in production.** |
| `FASTAPI_USERS_RESET_PASSWORD_TOKEN_SECRET` | cognee core | `super_secret` | Password-reset token secret. **Change in production.** |
| `DEFAULT_USER_EMAIL` | cognee core | _(unset)_ | Default admin user email seeded on first boot. |
| `DEFAULT_USER_PASSWORD` | cognee core | _(unset)_ | Default admin user password seeded on first boot. |

---

## 18. Chunking & Graph Enrichment

| Variable | Integration | Default | Description |
|---|---|---|---|
| `CHUNK_SIZE` | cognee core | `1500` | Target chunk size in tokens. |
| `CHUNK_OVERLAP` | cognee core | `10` | Token overlap between adjacent chunks. |
| `CHUNK_STRATEGY` | cognee core | `paragraph` | Chunking strategy (`paragraph`, `sentence`, etc.). |
| `TRIPLET_EMBEDDING` | cognee core | `false` | Embed triplet-level vectors during cognify. |
| `RAISE_INCREMENTAL_LOADING_ERRORS` | cognee core | `true` | Raise errors during incremental graph loading instead of silently skipping. |

---

## 19. Translation

| Variable | Integration | Default | Description |
|---|---|---|---|
| `TRANSLATION_PROVIDER` | cognee core | `llm` | Translation provider: `llm`, `google`, `azure`. |
| `TARGET_LANGUAGE` | cognee core | `en` | Target language for non-English ingestion. |
| `CONFIDENCE_THRESHOLD` | cognee core | `0.8` | Minimum confidence for language detection. |
| `GOOGLE_TRANSLATE_API_KEY` | cognee core | _(unset)_ | Google Cloud Translation API key. |
| `GOOGLE_PROJECT_ID` | cognee core | _(unset)_ | Google Cloud project ID for translation. |
| `AZURE_TRANSLATOR_KEY` | cognee core | _(unset)_ | Azure Cognitive Translator API key. |
| `AZURE_TRANSLATOR_REGION` | cognee core | _(unset)_ | Azure Translator region (e.g. `westeurope`). |
| `AZURE_TRANSLATOR_ENDPOINT` | cognee core | _(unset)_ | Azure Translator endpoint URL. |
| `TRANSLATION_BATCH_SIZE` | cognee core | `10` | Batch size for translation requests. |
| `TRANSLATION_MAX_RETRIES` | cognee core | `3` | Max retry attempts. |
| `TRANSLATION_TIMEOUT_SECONDS` | cognee core | `30` | Per-request timeout. |

---

## 20. Observability & Tracing

| Variable | Integration | Default | Description |
|---|---|---|---|
| `COGNEE_TRACING_ENABLED` | cognee core | _(unset)_ | Set `true` to enable OpenTelemetry trace export. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | cognee core | _(unset)_ | OTLP backend endpoint (Dash0, Grafana, Jaeger). |
| `OTEL_EXPORTER_OTLP_HEADERS` | cognee core | _(unset)_ | Auth headers for OTLP backend. |
| `OTEL_SERVICE_NAME` | cognee core | `cognee` | Service name reported in traces. |
| `OTEL_RESOURCE_ATTRIBUTES` | cognee core | _(unset)_ | Extra OTEL resource attributes. |
| `MONITORING_TOOL` | cognee core | _(unset)_ | Set `langfuse` to enable LLM call tracing. |
| `LANGFUSE_PUBLIC_KEY` | cognee core | _(unset)_ | Langfuse public key. |
| `LANGFUSE_SECRET_KEY` | cognee core | _(unset)_ | Langfuse secret key. |
| `LANGFUSE_HOST` | cognee core | _(unset)_ | Langfuse server URL. |

---

## 21. Migration

| Variable | Integration | Default | Description |
|---|---|---|---|
| `ENABLE_AUTO_MIGRATIONS` | cognee core | `true` | Run graph/vector migrations automatically on startup. Set `false` for operator-driven upgrades. |
| `MIGRATION_DB_PATH` | cognee core | _(unset)_ | Directory for the relational → graph migration DB. |
| `MIGRATION_DB_NAME` | cognee core | `migration_database.sqlite` | Migration DB filename. |
| `MIGRATION_DB_PROVIDER` | cognee core | `sqlite` | Migration DB provider. |
| `MIGRATION_DB_USERNAME` | cognee core | _(unset)_ | Migration DB username (postgres). |
| `MIGRATION_DB_PASSWORD` | cognee core | _(unset)_ | Migration DB password (postgres). |
| `MIGRATION_DB_HOST` | cognee core | _(unset)_ | Migration DB host (postgres). |
| `MIGRATION_DB_PORT` | cognee core | _(unset)_ | Migration DB port (postgres). |

---

## 22. BAML Structured Output

Only relevant when `STRUCTURED_OUTPUT_FRAMEWORK=baml`.

| Variable | Integration | Default | Description |
|---|---|---|---|
| `BAML_LLM_PROVIDER` | cognee core | _(unset)_ | BAML LLM provider. |
| `BAML_LLM_MODEL` | cognee core | _(unset)_ | BAML model name. |
| `BAML_LLM_ENDPOINT` | cognee core | _(unset)_ | BAML LLM endpoint. |
| `BAML_LLM_API_KEY` | cognee core | _(unset)_ | BAML API key. |
| `BAML_LLM_API_VERSION` | cognee core | _(unset)_ | BAML API version. |

---

## 23. Local llama.cpp Provider

| Variable | Integration | Default | Description |
|---|---|---|---|
| `LLAMA_CPP_MODEL_PATH` | cognee core | _(unset)_ | Path to a GGUF model file. |
| `LLAMA_CPP_N_CTX` | cognee core | `2048` | Context window size. |
| `LLAMA_CPP_N_GPU_LAYERS` | cognee core | `0` | Layers to offload to GPU. |
| `LLAMA_CPP_CHAT_FORMAT` | cognee core | `chatml` | Chat prompt format. |

---

## 24. Web Scraper & Misc

| Variable | Integration | Default | Description |
|---|---|---|---|
| `WEB_SCRAPER_TIMEOUT` | cognee core | `15.0` | Per-page HTTP timeout (seconds). |
| `WEB_SCRAPER_MAX_DELAY` | cognee core | `10.0` | Max random delay between scrape requests (seconds). |
| `DLT_MAX_ROWS_PER_TABLE` | cognee core | _(unset)_ | Max rows per table for DLT ingestion. |
| `COGNEE_CLOUD_API_URL` | cognee core | `http://localhost:8001` | Cognee Cloud sync API URL. |
| `COGNEE_CLOUD_AUTH_TOKEN` | cognee core | _(unset)_ | Auth token for Cognee Cloud sync. |
| `UI_APP_URL` | cognee core | `http://localhost:3000` | Frontend UI URL (CORS / redirect target). |
| `ENABLE_LAST_ACCESSED` | cognee core | `false` | Track last-accessed timestamps on graph nodes. |
| `ONTOLOGY_RESOLVER` | cognee core | _(unset)_ | OWL ontology resolver (e.g. `rdflib`). |
| `MATCHING_STRATEGY` | cognee core | _(unset)_ | Entity matching strategy (e.g. `fuzzy`). |
| `ONTOLOGY_FILE_PATH` | cognee core | _(unset)_ | Full path to the OWL ontology file. |

---

## 25. n8n_workflows — Advanced Self-Improve Runner

Variables consumed by `n8n_workflows/cognee_skill_self_improve/advanced/run_self_improve_skill.py` and `run_n8n_action.sh`. None are required for the **beginner** (Verified Node) build.

| Variable | Default | Description |
|---|---|---|
| `COGNEE_SELF_IMPROVE_WORKFLOW_ROOT` | _(cwd fallback)_ | Absolute path to the `advanced/` directory. Required when n8n is started from a different working directory. |
| `COGNEE_REPO` | _(unset)_ | Path to the local cognee repo root (used to find the venv Python). |
| `COGNEE_PYTHON` | `$COGNEE_REPO/.venv/bin/python` → `python3` → `python` | Python interpreter used by `run_n8n_action.sh`. First non-empty of: `COGNEE_PYTHON`, `$COGNEE_REPO/.venv/bin/python`, `python3`, `python`. |
| `COGNEE_SELF_IMPROVE_SMOKE` | _(unset)_ | Set `1` to initialize Cognee and ingest skills only (no agent run). |
| `COGNEE_SELF_IMPROVE_PRUNE` | _(unset)_ | Set `1` to clear Cognee data/system metadata before the run. |
| `COGNEE_SELF_IMPROVE_APPLY` | `1` | Set `0` to propose but not apply the improvement. |
| `COGNEE_SELF_IMPROVE_SYNC_FILE` | `1` | Set `0` to apply in graph only; do not rewrite `SKILL.md`. |
| `COGNEE_SELF_IMPROVE_APPROVED` | `1` | Set `0` to generate a review packet but not apply. |
| `COGNEE_SELF_IMPROVE_SYSTEM_ROOT` | _(workflow-local `.cognee_system`)_ | Override workflow-local system storage path. |
| `COGNEE_SELF_IMPROVE_DATA_ROOT` | _(workflow-local `.cognee_data`)_ | Override workflow-local data storage path. |
| `COGNEE_SKILL_SCORE` | `0.3` | Evaluator score recorded in `SkillRunEntry`. |
| `COGNEE_SKILL_SCORE_FROM_AGENT` | _(unset)_ | Set `1` to use the agent's JSON score instead of `COGNEE_SKILL_SCORE`. |
| `COGNEE_SKILL_SCORE_THRESHOLD` | `0.9` | Threshold below which proposals are created. |

---

## 26. n8n Integration (Verified Node)

The `integrations/n8n/` community node **does not consume shell environment variables**. All connection settings are entered through n8n's credential UI:

| Credential field | Description |
|---|---|
| **Base URL** | Cognee API base URL (e.g. `https://tenant-xxx.aws.cognee.ai`). The node appends `/api` automatically; do not include a trailing `/api`. |
| **API Key** | Cognee API key — sent as the `X-Api-Key` header on every request. |

---

## Quick-start Cheat Sheet

### Cloud / remote mode (all integrations)
```bash
export COGNEE_BASE_URL="https://your-instance.cognee.ai"
export COGNEE_API_KEY="ck_..."
```

### Local mode — claude-code / codex
```bash
export LLM_API_KEY="sk-..."          # only required variable
# Optional overrides
export COGNEE_PLUGIN_DATASET="my-project"
export COGNEE_SESSION_ID="my-project"  # share session across terminals
```

### Local mode — hermes-agent
```bash
export LLM_API_KEY="sk-..."
export LLM_MODEL="openai/gpt-4o-mini"
export COGNEE_DATASET="hermes"
# Optional: point at a shared server
# export COGNEE_BASE_URL="http://localhost:8000"
```

### Docker (openclaw / standalone)
```bash
export LLM_API_KEY="sk-..."
docker compose -f integrations/openclaw/cognee-docker-compose.yaml up -d
# Then set in openclaw config: baseUrl: http://localhost:8000
```

### n8n advanced workflow runner
```bash
export COGNEE_SELF_IMPROVE_WORKFLOW_ROOT="$PWD/n8n_workflows/cognee_skill_self_improve/advanced"
export COGNEE_REPO="/path/to/cognee"     # to locate the SDK venv
export LLM_API_KEY="sk-..."
NODES_EXCLUDE=[] N8N_PORT=5680 npx n8n
```
