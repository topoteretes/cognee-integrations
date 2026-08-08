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
- Closes every session out of process, the way the other cognee plugins do: a
  detached worker bridges the session into the graph and only then unregisters
  from the server. Exiting Hermes never waits on a graph build, and the promotion
  is never cut short by the server retiring. The same worker covers an uncleanly
  died Hermes, so no session is lost and no server lingers either way.

## Quick start

### Prerequisites

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed
  (`curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`).
- **Local mode:** an LLM API key (e.g. OpenAI) — cognee uses it to build the
  knowledge graph on your machine.
- **Cloud mode:** a Cognee Cloud tenant URL and API key from your
  [Cognee Cloud dashboard](https://platform.cognee.ai/). No LLM key needed —
  the tenant runs the models.

### 1. Install the plugin

Via pip (recommended):

```bash
pip install cognee-integration-hermes-agent
cognee-hermes-install
```

Hermes discovers plugins by scanning `~/.hermes/plugins/`, so the second
command copies the plugin there — `pip install` alone is not enough, and after
a `pip install -U` you re-run `cognee-hermes-install` to update the copy
(`hermes cognee status` reminds you when the two drift).

Or from a checkout of this repository:

```bash
git clone https://github.com/topoteretes/cognee-integrations.git
mkdir -p ~/.hermes/plugins/cognee
cp -R cognee-integrations/integrations/hermes-agent/. ~/.hermes/plugins/cognee/
```

### 2a. Connect locally (default)

```bash
hermes memory setup
```

Select `cognee` in the provider picker, choose **Mode: local**, and paste your
LLM API key when asked. That's the whole setup — the wizard writes non-secrets
to `~/.hermes/cognee.json` and secrets to `~/.hermes/.env`.

On your next `hermes` session the plugin starts a cognee server on
`127.0.0.1:8011` — or attaches to one that a sibling cognee plugin (Claude
Code, Codex, OpenClaw) already runs — with storage in `~/.cognee`. The very
first boot runs database migrations and can take a couple of minutes; after
that it's instant.

Verify it's connected:

```bash
hermes cognee status                    # shows mode, dataset, service URL
curl -s http://127.0.0.1:8011/health    # the server answers
```

Then, in a `hermes` chat: *"Remember that my favorite editor is Helix"* — the
agent should call `cognee_remember`. Start a fresh conversation (`/new`) and
ask *"What's my favorite editor?"* — it should recall it via `cognee_recall`.

### 2b. Connect to Cognee Cloud

Grab your tenant URL (`https://tenant-xxx.aws.cognee.ai`) and an API key from
the [Cognee Cloud dashboard](https://platform.cognee.ai/), then run the same
wizard and choose **Mode: remote**:

```bash
hermes memory setup     # cognee -> Mode: remote -> tenant URL + API key
```

Verify: `hermes cognee status` shows your tenant URL, and the same
remember-`/new`-recall chat round trip works. Nothing runs locally in this
mode — no server is spawned and no LLM key is used; every request goes to the
tenant, authenticated with your API key via `X-Api-Key`.

> **Switching modes? Re-run the wizard.** Values in `~/.hermes/cognee.json`
> take precedence over environment variables, and a local setup records
> `"service_url": ""` there — so *only* exporting `COGNEE_BASE_URL` will not
> move an existing local install to the cloud. `hermes memory setup` (or
> `hermes cognee setup`) rewrites both files consistently.

## How the pip install works

Hermes has no entry-point plugin discovery (yet) — it scans
`$HERMES_HOME/plugins/` for directories with a `plugin.yaml`. The wheel
therefore ships the plugin-root files as package data and provides the
`cognee-hermes-install` console script, which materializes the exact directory
shape the scanner expects. Because Hermes runs that *copy*, upgrading is always
two steps: `pip install -U cognee-integration-hermes-agent`, then
`cognee-hermes-install` again.

The package also declares the entry point Hermes would use if it grows native
discovery, at which point the copy step becomes unnecessary:

```toml
[project.entry-points."hermes_agent.plugins"]
cognee = "cognee_integration_hermes"
```

Releases are published from CI on `hermes-agent-v*` tags
(`.github/workflows/hermes-agent-publish.yml`).

## Configuration

The quick start above covers the common cases; this section is the full
reference. Configuration comes from two places: `$HERMES_HOME/.env` (secrets
and environment variables — Hermes loads it for every session) and
`$HERMES_HOME/cognee.json` (non-secret settings). The setup wizard writes both.
When a key appears in both places, **the JSON file wins** — which is why mode
switches should go through the wizard rather than editing the environment
alone.

## Modes

The provider connects to Cognee in one of three modes. It picks the mode
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
And if initialization does fail, memory stays *off*: Hermes logs the error and
starts anyway, so the provider refuses every call rather than operating a
half-connected backend.

### Transports

Mode decides *where* cognee is; the transport decides *how* the plugin talks to it.

| Transport | Selected by | What it does |
| --- | --- | --- |
| **http** (default) | nothing to set | builds requests against cognee's REST API directly, using only the standard library |
| **sdk** | `COGNEE_TRANSPORT=sdk`, or any `COGNEE_EMBEDDED=true` | drives the `cognee` Python package, via `cognee.serve()` when a server is involved |

Direct HTTP is the default because it is what the Claude Code, Codex and OpenClaw
plugins do, and because the SDK's `CloudClient` drops fields the server accepts —
most importantly `session_ids` on `improve()`, which is what promotes a session's
turns into the permanent graph. Two consequences worth knowing:

- The `cognee` package is still required. It is what the local server runs, and it
  is the only way to run without a server at all (`COGNEE_EMBEDDED=true`).
- Over HTTP, a `cognee_remember` write cannot be linked to the session it came
  from — `/api/v1/remember` has no field for it. Session-to-graph bridging is
  unaffected. The plugin logs this once rather than dropping it silently.

> **Upgrading from 0.1.x — three defaults moved to match the other cognee
> plugins.** The local port changed from 8000 to 8011 (leaving cognee's own
> default of 8000 to servers you start yourself); the default dataset changed
> from `hermes` to the shared `agent_sessions`; and local storage now defaults to
> the shared `~/.cognee/{data,system}` instead of cognee's global default. Your
> old memory is not deleted, but a recall against the new dataset/roots will not
> see it — set `COGNEE_DATASET=hermes` (or migrate the data) and, if an old
> plugin-started server is still listening on 8000, **stop it**: two servers
> sharing one data directory is exactly the single-writer contention this mode
> exists to avoid. `COGNEE_LOCAL_PORT=8000` restores the old port.

### One brain across agents

By default this plugin joins the same memory the Claude Code, Codex and OpenClaw
cognee plugins share: the same dataset (`agent_sessions`), the same local storage
(`~/.cognee/{data,system}`), the same server port (8011) and the same minted API
key (`~/.cognee-plugin/api_key.json`). Whichever plugin boots the server first,
the rest attach to it — and a fact remembered in Claude Code is recallable in
Hermes, and vice versa. To keep Hermes (or one Hermes profile) apart instead, give
it its own `COGNEE_PLUGIN_DATASET`, or for full isolation its own
`COGNEE_DATA_ROOT` / `COGNEE_SYSTEM_ROOT` *and* `COGNEE_LOCAL_PORT` — a server
belongs to whoever reaches its port first, so a private store needs a private
port.

The per-mode settings below live in `~/.hermes/.env` (the wizard puts them
there; you can also edit the file by hand).

local-server mode (default — just set your LLM creds):

```bash
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
# COGNEE_PLUGIN_DATASET=agent_sessions   # optional; the default is shared with the other plugins
# COGNEE_LOCAL_PORT=8011                 # optional; the other plugins' server port
```

Remote / cloud mode (tenant URL and API key from the
[Cognee Cloud dashboard](https://platform.cognee.ai/)):

```bash
COGNEE_BASE_URL=https://tenant-xxx.aws.cognee.ai   # canonical name
COGNEE_API_KEY=...
```

> **`COGNEE_API_KEY` is mandatory for any remote server.** On a local server the
> plugin mints a key on first use (a one-time login as the default user); remote
> servers — Cognee Cloud included — expose no login route, so there is nothing to
> mint with. A remote `COGNEE_BASE_URL` without a key fails at startup with a
> clear error rather than a 401 on every call.

Embedded (in-process) mode — single-process / offline only:

```bash
COGNEE_EMBEDDED=true
LLM_API_KEY=sk-...
```

> **Embedded mode and the shared store do not mix.** Embedded drives the local
> single-writer databases from inside the Hermes process; if another plugin's
> server (or another process) is using `~/.cognee` at the same time, that is
> exactly the contention embedded mode is warned about. For embedded use, point
> `COGNEE_DATA_ROOT` / `COGNEE_SYSTEM_ROOT` at a private location.

### Optional settings

| Setting | Env var | Default |
| --- | --- | --- |
| `dataset` | `COGNEE_PLUGIN_DATASET` (canonical) | `agent_sessions` |
| `top_k` | `COGNEE_TOP_K` | `5` |
| `auto_route` | `COGNEE_AUTO_ROUTE` | `true` |
| `improve_on_end` | `COGNEE_IMPROVE_ON_END` | `true` |
| `improve_background` | `COGNEE_IMPROVE_BACKGROUND` | auto |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `hermes` |
| `service_url` | `COGNEE_BASE_URL` (canonical) | empty |
| `embedded` | `COGNEE_EMBEDDED` | `false` |
| `local_port` | `COGNEE_LOCAL_PORT` | `8011` |
| `server_boot_timeout` | `COGNEE_SERVER_BOOT_TIMEOUT` | `600` |
| `data_root` | `COGNEE_DATA_ROOT` | `~/.cognee/data` |
| `system_root` | `COGNEE_SYSTEM_ROOT` | `~/.cognee/system` |

> **Storage is shared, and a server is per port.** The roots above are the ones
> every cognee agent plugin pins, so the store is the same no matter which plugin
> booted the server on 8011. Because the default roots live outside
> `HERMES_HOME`, `backup_paths()` reports them to `hermes backup` — a profile
> backup deliberately includes the machine's shared memory store. Roots you point
> elsewhere with `COGNEE_DATA_ROOT` / `COGNEE_SYSTEM_ROOT` are reported the same
> way (unless they sit inside `HERMES_HOME`, which `hermes backup` walks anyway).

> `COGNEE_SERVICE_URL` is a deprecated alias for `COGNEE_BASE_URL`, and
> `COGNEE_DATASET` (the 0.1.x name) a lower-precedence alias for
> `COGNEE_PLUGIN_DATASET`. Both still work; new setups should use the canonical
> names.

> **`improve_background`** decides where the session-end graph build
> (`improve()`) runs. Default `auto`: whenever a server is involved, the close is
> handed to a **detached worker** — the same process that already covers crashes —
> which runs `improve()` to completion and only then unregisters the agent
> connection. Hermes exits immediately; nothing waits on the graph build. That
> ordering is required, not stylistic: the local server runs with
> `COGNEE_AGENT_MODE=true` and retires itself within 60s of the last agent
> unregistering, so unregistering first would kill the promotion halfway. In
> `embedded` mode there is no server and no worker, so the build runs in-process
> and synchronously — it dies with the process otherwise.
>
> Setting `COGNEE_IMPROVE_BACKGROUND=true|false` opts out of the handoff and does
> the work in-process: `true` submits the build and returns (right for a
> cloud/remote server nothing here can shut down; on a local server it
> reintroduces the race above), `false` blocks Hermes' exit until the build
> finishes.

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
