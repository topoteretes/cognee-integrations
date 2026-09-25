<div align="center">
  <a href="https://www.cognee.ai">
    <img src="https://raw.githubusercontent.com/topoteretes/cognee-integrations/main/assets/cognee-logo.svg" alt="Cognee" width="260">
  </a>
  <p><strong>Cognee memory for Hermes Agent</strong> — persistent, graph-backed memory with session recall, durable remember/forget, and automatic capture for your Hermes agents.</p>
  <p>
    <a href="https://docs.cognee.ai">Docs</a> ·
    <a href="https://discord.gg/NQPKmU5CCg">Discord</a> ·
    <a href="https://github.com/topoteretes/cognee">Cognee core</a>
  </p>
  <p>
    <a href="https://pypi.org/project/cognee-integration-hermes-agent/"><img src="https://img.shields.io/pypi/v/cognee-integration-hermes-agent" alt="PyPI version"></a>
    <a href="https://pypi.org/project/cognee-integration-hermes-agent/"><img src="https://img.shields.io/pypi/dm/cognee-integration-hermes-agent" alt="PyPI downloads"></a>
  </p>
</div>

# Cognee Memory Plugin for Hermes Agent

Standalone Hermes memory provider backed by Cognee.

This replaces the closed in-tree Hermes PR path. Hermes no longer accepts new
providers under `plugins/memory/`; this integration is shaped as a standalone
plugin that can be installed into `~/.hermes/plugins/cognee` or distributed as a
Python package with the `hermes_agent.plugins` entry point.

## Features

- Stores each completed Hermes turn in Cognee session memory.
- Recalls memory per prompt with a single request: one `<cognee_memory>`
  block holding the session history, retrieved graph context and session
  guidance (see [Per-prompt memory block](#per-prompt-memory-block)), with a
  plain-words hit counter on top.
- Uses `cognee_recall` for explicit search, `cognee_remember` for durable
  graph memory.
- Exposes `cognee_forget` for user-directed, per-document deletion ("forget
  what we said about tennis"): find candidates with previews, then delete only
  what the user confirms.
- Exposes `cognee_switch_dataset` to move a conversation to another dataset
  mid-session, bridging the session it leaves behind.
- Indexes repositories into a deterministic code graph (`hermes cognee
  index-repo`) and answers structural code questions exactly via
  `cognee_code_search`, plus an identifier-gated code recall lane.
- Runs `cognee.improve()` at Hermes session end to bridge session memory into the graph.
- Mirrors explicit Hermes memory writes through `on_memory_write`, and steers
  the agent to prefer Cognee over Hermes' built-in memory files.
- Supports local embedded Cognee and remote Cognee service mode.
- Closes every session out of process, the way the other cognee plugins do: a
  detached worker bridges the session into the graph and only then unregisters
  from the server. Exiting Hermes never waits on a graph build, and the promotion
  is never cut short by the server retiring. The same worker covers an uncleanly
  died Hermes, so no session is lost and no server lingers either way.

## Quick start

### Prerequisites

- **Python 3.10 or newer.** This integration imports cognee in-process
  (`requires-python = ">=3.10"` in `pyproject.toml`), so it inherits cognee's
  own floor; `pip` refuses to install it on 3.9. Note that macOS's Xcode
  Command Line Tools ship Python 3.9.6 — use a Homebrew, python.org or
  [uv](https://docs.astral.sh/uv/)-managed 3.10+ interpreter instead.
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed — see
  the [Hermes installation guide](https://hermes-agent.nousresearch.com/docs/getting-started/installation).
  (The install one-liner is quoted there rather than here: the catalog's install
  scanner flags a piped shell script even inside a README, and a clean scan is
  one less thing for a catalog reviewer to read past.)
- **Local mode:** an LLM API key (e.g. OpenAI) — cognee uses it to build the
  knowledge graph on your machine.
- **Cloud mode:** a Cognee Cloud tenant URL and API key from your
  [Cognee Cloud dashboard](https://platform.cognee.ai/). No LLM key needed —
  the tenant runs the models.

### 1. Install the plugin

**Hermes catalog — available after catalog acceptance:**

```bash
hermes plugins install cognee
hermes plugins enable cognee
hermes memory setup
```

Catalog installs use the commit reviewed by Hermes. Update them with
`hermes plugins update cognee`; a newer PyPI release does not change the
reviewed catalog version. The pip installer refuses to overwrite a catalog copy.

**Via pip (available now):**

```bash
pip install cognee-integration-hermes-agent
cognee-hermes-install
```

The pip package registers the memory provider through Hermes' entry-point
discovery. The second command copies it into `~/.hermes/plugins/cognee/` to
also provide the CLI and dashboard integration. For this installation method,
update with `pip install -U cognee-integration-hermes-agent` followed by
`cognee-hermes-install` (`hermes cognee status` reminds you when the two drift).

Or, for development, copy a checkout into a Hermes home with no existing
Cognee installation (do not copy over a catalog-managed plugin):

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

Hermes discovers memory providers two ways, and the package serves both:

- **Pip entry point** — the wheel declares
  `[project.entry-points."hermes_agent.memory_providers"]`, the group Hermes'
  memory loader scans, so the provider activates from a plain `pip install`.
- **Directory install** — `cognee-hermes-install` copies the plugin into
  `$HERMES_HOME/plugins/cognee/` in the exact shape the directory scanner
  expects; the load-bearing file is the root `__init__.py` (Hermes silently
  skips a plugin directory without one). The directory install is the
  recommended path: it carries the `hermes cognee` subcommands and the
  dashboard config panel at full fidelity.

For a pip-managed directory *copy*, upgrading takes two steps:
`pip install -U cognee-integration-hermes-agent`, then `cognee-hermes-install`
again (`hermes cognee status` reminds you when the copy is stale). Catalog
installations instead use `hermes plugins update cognee`.

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

> **Where the default user's password comes from.** cognee >= 1.6.0 ships no
> built-in default-user password: the API server creates the default user at
> startup only when `DEFAULT_USER_PASSWORD` is set in *its* environment. The
> local server this plugin spawns always gets `DEFAULT_USER_EMAIL=default_user@example.com`
> / `DEFAULT_USER_PASSWORD=default_password` — the same literals the Claude Code,
> Codex and Antigravity plugins pass, since they all share this server — unless
> you export `DEFAULT_USER_EMAIL` / `DEFAULT_USER_PASSWORD` yourself, in which
> case your values win. `COGNEE_USER_EMAIL` / `COGNEE_USER_PASSWORD` do *not*
> change the server's default user: they only select which user the plugin logs
> in as to mint its key, and a non-default user must already exist on the server.
> If you point the plugin at a local server you start yourself, start it with
> `DEFAULT_USER_PASSWORD` set (matching `COGNEE_USER_PASSWORD` if you changed
> that), or set `COGNEE_API_KEY` to a key that server issued. The server sets
> the password once and never rewrites an existing user's, so a later change has
> to be made on the server as well. A login the server refuses
> (`This user does not have a password` / `LOGIN_BAD_CREDENTIALS`) is logged as
> a warning at startup and repeated on the first `401`, naming the fix.

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
| `recall_timeout` | `COGNEE_RECALL_TIMEOUT` | `120` (seconds) |
| `write_timeout` | `COGNEE_WRITE_TIMEOUT` | `120` (seconds) |
| `improve_timeout` | `COGNEE_IMPROVE_TIMEOUT` | `300` (seconds) |
| `recall_budget` | `COGNEE_RECALL_BUDGET` | `20` (seconds, bounds the per-prompt recall) |
| `memory_steer` | `COGNEE_MEMORY_STEER` | `true` |
| `memory_steer_text` | `COGNEE_MEMORY_STEER_TEXT` | built-in wording |
| `memory_hits` | `COGNEE_MEMORY_HITS` | `true` |
| `dataset_switch_tool` | `COGNEE_DATASET_SWITCH_TOOL` | `true` |
| `code_search_tool` | `COGNEE_CODE_SEARCH_TOOL` | `true` |
| `code_graph_recall` | `COGNEE_CODE_GRAPH_RECALL` | `true` |
| `code_datasets` | `COGNEE_CODE_DATASETS` | empty (comma-separated extra code datasets) |
| `update_check` | `COGNEE_UPDATE_CHECK` | `true` (CLI-only PyPI check) |
| `update_check_interval` | `COGNEE_UPDATE_CHECK_INTERVAL` | `3600` (seconds) |

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

## Local models via Ollama: embedding settings

Ollama embeddings need cognee's `ollama` extra — `pip install "cognee[ollama]"`
— which brings the `transformers` package the token counting depends on; with
plain `cognee` the embedding engine fails to construct at all.

cognee reads these standard variables (put them in `$HERMES_HOME/.env` — Hermes
loads it every session, so the spawned server inherits them; see
[.env.example](./.env.example)):

| Env var | Meaning |
| --- | --- |
| `EMBEDDING_PROVIDER` | `ollama` for a local embedder |
| `EMBEDDING_MODEL` | e.g. `all-minilm`, `nomic-embed-text` |
| `EMBEDDING_ENDPOINT` | usually `http://localhost:11434/api/embed` |
| `EMBEDDING_DIMENSIONS` | the model's vector size (e.g. `384` for all-minilm) |
| `EMBEDDING_MAX_COMPLETION_TOKENS` | **must be ≤ the model's context length** |
| `HUGGINGFACE_TOKENIZER` | the HF tokenizer matching the model, used to count tokens |

**Why the token ceiling matters.** cognee sizes its text chunks from
`EMBEDDING_MAX_COMPLETION_TOKENS`, and its default (8191) is far above any local
embedding model's real context. Every substantial document then overflows the
model; cognee splits the text and **mean-pools the vectors while still reporting
success**, so the pipeline completes but the search index quietly fills with
lossy embeddings and retrieval degrades — the only trace is an
`Ollama embedding error` line in `~/.cognee-plugin/hermes/server.log`. Worse,
recent Ollama versions default to *silently truncating* oversized inputs, so
depending on the Ollama version the index degrades with no log line at all —
which is why a correct token ceiling matters even when the log is clean.

The plugin therefore pins safe defaults at server spawn when
`EMBEDDING_PROVIDER=ollama`: a context-matched
`EMBEDDING_MAX_COMPLETION_TOKENS` (a conservative 512 for models it does not
recognize) and, for recognized models, the matching `HUGGINGFACE_TOKENIZER`.
Explicit values always win over the pins. When the plugin detects an overflow in
the server log anyway, recall/remember results carry a `warning`/`error` naming
these levers.

> **Pins apply at server spawn.** A cognee server already running on the port
> keeps the environment it was started with — after changing embedding settings,
> stop that server (it is shared with the other cognee plugins) so the next
> session respawns it. An index written with wrong settings stays wrong until
> the dataset is rebuilt: follow [RUNBOOK.md](./RUNBOOK.md).

**If recall is slow or times out**: the default `GRAPH_COMPLETION` search runs
an LLM per query, which local models make slow. `search_type=CHUNKS` returns
matching stored text directly with no LLM in the loop; `COGNEE_RECALL_TIMEOUT`
raises the deadline.

### Per-prompt memory block

Every prompt triggers exactly one memory request: `scope=["graph"]`,
`search_type=HYBRID_COMPLETION`, `only_context=true`, with the conversation's
`session_id` (plus a separate deterministic code-graph request when the
[code recall lane](#code-graph-index-a-repository) is armed). No LLM is called
on the server for it. On cognee >= 1.6.0 the server answers with one graph
item per dataset whose `text` is the full input the completion would have
received — the session's conversation history, the question with the retrieved
context, then the session guidance block — and the plugin injects that string
verbatim as the `<cognee_memory>` block, untruncated. Older servers (1.5.x)
return the bare retrieval context in `text`, which is injected the same way.
The item's `system_prompt` field is ignored.

Memory is read from the knowledge graph only, on the per-prompt block and on the
explicit `cognee_recall` tool alike: the server's session-cache scopes
(`session`, `trace`, `session_context`) and its `auto` scope, which folds them
in, are never requested. Turns are still written to the session cache — that is
what `improve()` promotes into the graph at session end — but they are not
searched as raw entries; on cognee >= 1.6.0 the graph item's prompt already
carries this conversation's history because the session id travels with every
recall. `cognee_recall` takes `query`, an optional `search_type` and `top_k`.

## Code graph: index a repository

Repositories are indexed explicitly (Hermes is rarely launched inside a
checkout, so there is no auto-indexing):

```bash
hermes cognee index-repo ~/work/my-service            # local path
hermes cognee index-repo https://github.com/o/repo    # URL (the server clones it)
hermes cognee index-repo ~/work/my-service --wait 120 # block until queryable
```

Each repository gets its own `codebase-<repo>-<digest>` dataset. Indexing is
deterministic — no LLM or embedding calls on either side (add semantic search
over code entities with `--index-vectors`). Requires a cognee server >= 1.5.4.

Once indexed, two things light up in a Hermes session:

- the **`cognee_code_search` tool** — exact structural answers: `query_facts`,
  `explore`, `traverse`, `find_path`, `impact_analysis`, `delta`;
- the **code recall lane** — a prompt naming an identifier-shaped token
  (`process_payment`, `UserService`, `billing/api.py`) while Hermes runs inside
  an indexed repo gets code-graph facts injected alongside the memory block.
  For repos indexed elsewhere, list their datasets in `COGNEE_CODE_DATASETS`.

A locally indexed path reflects the working tree at index time; a URL-indexed
repo reflects the last pushed commit. Re-run `index-repo` after significant
changes — the server's content hashes make re-runs cheap.

## Hermes Commands

When Cognee is the active memory provider:

```bash
hermes cognee status [--check-updates]
hermes cognee version [--check-updates]
hermes cognee setup
hermes cognee config
hermes cognee install
hermes cognee index-repo <path-or-url> [--dataset D] [--index-vectors] [--wait SECONDS]
```

For pip installations, `status` and `version` include an update hint when PyPI has a newer release
(checked at most once per `COGNEE_UPDATE_CHECK_INTERVAL`, never from a live
session): update with `pip install -U cognee-integration-hermes-agent` and then
`cognee-hermes-install`, since Hermes runs the installed copy.

For catalog installations, these commands show the running plugin version and
direct you to `hermes plugins update cognee`. They never query PyPI, even with
`--check-updates`; that flag prints catalog update guidance rather than checking
for a new catalog release. `hermes plugins update cognee` checks the catalog.

## Development

```bash
cd integrations/hermes-agent
uv sync --dev
uv run pytest -q
uv run ruff check .
```
