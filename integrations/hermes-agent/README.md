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

### Modes

The provider connects to cognee in one of three modes. It picks the mode
automatically from your config:

| Mode | When it's used | How it talks to cognee |
| --- | --- | --- |
| **local-server** (default) | no `COGNEE_BASE_URL`, `COGNEE_EMBEDDED` unset | ensures a local cognee server is running and connects as a thin client |
| **remote** | `COGNEE_BASE_URL` is set | thin client to your managed / cloud cognee |
| **embedded** | `COGNEE_EMBEDDED=true` | runs cognee in-process |

**Why local-server is the default.** cognee's local stores (SQLite, Kuzu/Ladybug,
LanceDB) are single-writer. Driving them in-process from the agent's background
threads — or from a second Hermes process sharing the same `data_root` — risks
`database is locked` errors and corruption. A local cognee server is the single
owner that serializes all access, so the agent just makes HTTP calls. This is the
same design the Claude Code and Codex plugins use. **`embedded` is opt-in and is
safe for single-process / offline use only.**

**No silent fallbacks.** The provider never downgrades modes behind your back. If
`COGNEE_BASE_URL` is set but unreachable, or the local server fails to start,
initialization raises rather than quietly switching to a different mode — silent
fallback would either mask a config error (remote → local data divergence) or
reintroduce the very DB-lock risk this design removes (local-server → embedded).
To accept the single-process trade-off, set `COGNEE_EMBEDDED=true` explicitly.

local-server mode (default — just set your LLM creds):

```bash
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
COGNEE_DATASET=hermes
# COGNEE_LOCAL_PORT=8000   # optional; point at a shared server for a unified brain
```

Remote / cloud mode:

```bash
COGNEE_BASE_URL=https://your-cognee-service.example   # canonical name
COGNEE_API_KEY=...
COGNEE_DATASET=hermes
```

Embedded (in-process) mode — single-process / offline only:

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

## Troubleshooting

For common setup and runtime failures, see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

## Development

```bash
cd integrations/hermes-agent
uv sync --dev
uv run pytest -q
uv run ruff check .
```

