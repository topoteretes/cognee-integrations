# Cognee Memory Plugin for Hermes Agent

Standalone Hermes memory provider backed by Cognee.

This replaces the closed in-tree Hermes PR path. Hermes no longer accepts new
providers under `plugins/memory/`; this integration is shaped as a standalone
plugin that can be installed into `~/.hermes/plugins/cognee` or distributed as a
Python package with the `hermes_agent.plugins` entry point.

## Features

- Stores each completed Hermes turn in Cognee session memory.
- Uses `cognee_recall` for session-first recall with graph fallback.
- Exposes `cognee_remember` for durable graph memory.
- Exposes `cognee_forget` for deletion requests.
- Runs `cognee.improve()` at Hermes session end to bridge session memory into the graph.
- Mirrors explicit Hermes memory writes through `on_memory_write`.
- Supports local embedded Cognee and remote Cognee service mode.

## Install For Local Hermes Development

From this repository:

```bash
mkdir -p ~/.hermes/plugins/cognee
cp -R integrations/hermes-agent/. ~/.hermes/plugins/cognee/
hermes memory setup
```

Select `cognee` in the memory provider picker.

## Install From Pip

```bash
pip install cognee-integration-hermes-agent
hermes memory setup
```

The package exposes:

```toml
[project.entry-points."hermes_agent.plugins"]
cognee = "cognee_integration_hermes"
```

## Configuration

The setup wizard writes non-secret settings to `$HERMES_HOME/cognee.json` and
secrets to `$HERMES_HOME/.env`.

### Runtime modes

Cognee integrations use the same runtime model:

| Mode | When to use it | How it talks to Cognee |
| --- | --- | --- |
| **local-server** (default) | You want local data with safe concurrent access | Starts or connects to a local Cognee server, then uses HTTP as a thin client |
| **cloud** | `COGNEE_BASE_URL` points to a managed or remote Cognee service | Uses HTTP as a thin client with `COGNEE_API_KEY` |
| **embedded** | You explicitly choose in-process Cognee for a single process or offline run | Runs Cognee inside the integration process |

**Why local-server is the safe default.** Cognee local stores, including SQLite, Kuzu, Ladybug, and LanceDB, are single-writer stores. If hooks, multiple terminals, or another integration use the same data root in embedded mode, they can hit `database is locked` errors or corrupt local state. A local Cognee server avoids that by owning the stores and serializing access. Each integration talks to it over HTTP.

**No silent fallbacks.** A configured cloud endpoint should fail clearly if it is unreachable. A local server should fail clearly if it cannot start. Falling back to another mode can hide configuration errors or write data to the wrong store. Use embedded mode only when you accept the single-process tradeoff.

### Runtime mode examples

local-server mode:

```bash
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
COGNEE_DATASET=hermes
# COGNEE_LOCAL_PORT=8000
```

cloud mode:

```bash
COGNEE_BASE_URL=https://your-cognee-service.example
COGNEE_API_KEY=...
COGNEE_DATASET=hermes
```

embedded mode:

```bash
COGNEE_EMBEDDED=true
LLM_API_KEY=sk-...
COGNEE_DATASET=hermes
```

### Optional settings

| Setting | Env var | Default |
| --- | --- | --- |
| `dataset` | `COGNEE_DATASET` | `hermes` |
| `top_k` | `COGNEE_TOP_K` | `5` |
| `auto_route` | `COGNEE_AUTO_ROUTE` | `true` |
| `improve_on_end` | `COGNEE_IMPROVE_ON_END` | `true` |
| `improve_background` | `COGNEE_IMPROVE_BACKGROUND` | auto |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `hermes` |
| `service_url` | `COGNEE_BASE_URL` (canonical) | empty |
| `embedded` | `COGNEE_EMBEDDED` | `false` |
| `local_port` | `COGNEE_LOCAL_PORT` | `8000` |
| `server_boot_timeout` | `COGNEE_SERVER_BOOT_TIMEOUT` | `30` |
| `data_root` | `COGNEE_DATA_ROOT` | `$HERMES_HOME/cognee/data` |
| `system_root` | `COGNEE_SYSTEM_ROOT` | `$HERMES_HOME/cognee/system` |

> `COGNEE_SERVICE_URL` is a deprecated alias for `COGNEE_BASE_URL`. It still works
> (with lower precedence) but new setups should use `COGNEE_BASE_URL`.

> **`improve_background`** controls whether the session-end graph build
> (`improve()`) runs in the background. Default `auto`: it backgrounds in
> server/remote mode (the server outlives the agent and finishes the job) and runs
> synchronously in `embedded` mode (the work runs in-process and must complete
> before shutdown, or it is lost). Set `COGNEE_IMPROVE_BACKGROUND=true|false` to
> force it. Previously `improve()` was always synchronous; this is the one
> behavior change to be aware of when upgrading.

## Hermes Commands

When Cognee is the active memory provider:

```bash
hermes cognee status
hermes cognee setup
hermes cognee config
hermes cognee install
```

## Development

```bash
cd integrations/hermes-agent
uv sync --dev
uv run pytest -q
uv run ruff check .
```

