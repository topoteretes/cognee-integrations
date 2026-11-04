# Cognee cassette for Paper's tapes

[Cognee](https://cognee.ai) memory as a first-class [tapes](https://tapes.dev)
**cassette**: an independent HTTP service that tapes discovers, validates, and
proxies under its own API namespace, following the
[`cassette/v1alpha1` contract](https://papercompute.com/blog/cassette-anatomy/).

It syncs recorded agent sessions from the tapes core API into a cognee
knowledge graph and answers questions over them — and exposes both operations
as MCP tools inside tapes, so agents can use their own session history as
memory.

## Service or one-shot sync

The package runs two ways, sharing one sync implementation (transcript rules,
state file, checkpoint):

| | Cassette service | One-shot sync |
|---|---|---|
| Command | `cognee-tapes-cassette` | `cognee-tapes-sync` |
| Shape | Long-running service, discovered by tapes | Runs one sync pass and exits (cron, scripts) |
| API surface | `/v1/cassettes/cognee/...` (proxied by tapes) | None |
| Agent access | MCP tools: `cognee.sync_sessions`, `cognee.sync_status`, `cognee.search_memory` | None |
| Needs tapes to load a cassette | Yes (`cassette/v1alpha1`) | No, only the tapes core API |

The one-shot sync is for deployments where tapes doesn't load cassettes, or
where a scheduled job is simpler than a service. Both use the transcript
extraction rules from the original tapes exporter (#362): only completed
sessions, only "main" LLM spans (no injected system context or
harness-internal offshoots), thinking blocks dropped, tool calls summarized to
a curated set of argument keys.

## Endpoints

The cassette serves (and tapes proxies under `/v1/cassettes/cognee/`):

| Route | MCP tool | What it does |
|---|---|---|
| `GET /ping` | — | Health check |
| `GET /openapi` | — | OpenAPI spec + `x-tapes-cassette` manifest (the contract) |
| `POST /api/sync` | `cognee.sync_sessions` | Incremental sync: list → export → ingest → cognify. Body: `{"full": bool, "wait": bool}` |
| `POST /api/sync/status` | `cognee.sync_status` | Current/last sync run snapshot |
| `POST /api/search` | `cognee.search_memory` | Search the session memory. Body: `{"query": str, "search_type"?: str, "top_k"?: int}` |

All MCP-exposed routes are `POST` because `v1alpha1` only converts `POST`
routes into tools.

`POST /api/sync` returns immediately and runs in the background by default
(tapes proxies have request timeouts; cognify can be slow). Pass
`{"wait": true}` to block until the run finishes and get its final status —
handy for scripts and small vaults. Sync is idempotent: a per-session content
hash skips unchanged sessions, and `cognify` is skipped when nothing new was
added (with a `pending_cognify` flag so an interrupted run finishes its
cognify next time).

## Incremental sync & the `last_seen_at` question

The original exporter (#362) read `last_seen_at` from the `/export` payload,
whose location there was never verified (every `/export` call 404'd during its
development). This package sidesteps that entirely: the checkpoint is computed from `last_seen_at` on
`GET /v1/sessions` **list items**, which the list endpoint is known to return.
Only completed sessions advance the checkpoint — an in-progress session's
`last_seen_at` bumps again when it completes, so the next incremental run
picks it up.

## Setup

Requires Python 3.10+, a running tapes instance, and an LLM API key
(`LLM_API_KEY`) for cognee's in-process LLM and embedding calls.

```bash
cd integrations/tapes-cassette
pip install -e .
cp .env.example .env   # fill in LLM_API_KEY
```

Run the cassette:

```bash
cognee-tapes-cassette
```

Register it with tapes:

```bash
tapes serve \
  --postgres postgres://tapes:tapes@localhost:5432/tapes?sslmode=disable \
  --cassettes localhost:9900/openapi
```

Smoke-test through the tapes proxy:

```bash
curl -X POST localhost:8081/v1/cassettes/cognee/api/sync -d '{"wait": true}' \
  -H 'content-type: application/json'
curl -X POST localhost:8081/v1/cassettes/cognee/api/search \
  -d '{"query": "what did we change about auth last week?"}' \
  -H 'content-type: application/json'
```

## One-shot sync (cron)

`cognee-tapes-sync` runs a single sync pass and exits, which suits cron or any
scheduler:

```bash
cognee-tapes-sync            # incremental sync from the saved checkpoint
cognee-tapes-sync --full     # ignore the checkpoint (unchanged sessions still skip)
cognee-tapes-sync --json     # print the status snapshot as JSON
```

Exit codes: `0` completed, `1` failed, `2` busy (another sync holds the run
lock). It reads the same settings as the service, from the environment or an
`.env` file (`--env-file`, default `./.env`; real environment variables win).

cron starts jobs in your home directory with a minimal environment, so use
absolute paths and give the job the same `CASSETTE_STATE_PATH` as any running
cassette. The state file and its run lock are what keep the two from ingesting
the same sessions twice or syncing at once:

```cron
*/15 * * * * cd /opt/cognee-tapes && CASSETTE_STATE_PATH=/opt/cognee-tapes/state.json .venv/bin/cognee-tapes-sync --env-file /opt/cognee-tapes/.env >> /var/log/cognee-tapes-sync.log 2>&1
```

## Configuration

| Setting | Env var | Default |
|---|---|---|
| Tapes core API base URL | `TAPES_BASE_URL` | `http://127.0.0.1:8081` |
| Cognee dataset name | `COGNEE_TAPES_DATASET` | `tapes_sessions` |
| Cassette listen host/port | `CASSETTE_HOST` / `CASSETTE_PORT` | `127.0.0.1` / `9900` |
| Sync state file | `CASSETTE_STATE_PATH` | `.cognee-cassette-state-<dataset>.json` |
| Forced cognee storage root | `COGNEE_STORAGE_ROOT` | unset (cognee defaults) |
| LLM API key (read by cognee) | `LLM_API_KEY` | unset |
| Log level | `LOG_LEVEL` | `INFO` (service), `WARNING` (one-shot sync) |

Set `COGNEE_STORAGE_ROOT` to keep the cassette's cognee data/system storage
isolated under one directory — recommended if your shell exports global cognee
storage variables.

Both commands also load an `.env` file from the working directory (the
one-shot sync takes `--env-file`); variables already set in the environment
take precedence.

The same settings are declared (with types and defaults) in the manifest's
`x-tapes-cassette.config` block, so tapes can introspect them.

## Known limitations

- **No authentication on tapes core API calls** — assumes a local, trusted
  tapes deployment.
- **Local state** — sync state lives in a local JSON file. A run lock next to it
  stops the service and the one-shot sync from syncing at once, but only when
  they share the same `CASSETTE_STATE_PATH`; different state files mean
  independent checkpoints and duplicate ingestion.
- **`v1alpha1` is alpha** — the manifest/MCP conventions follow the spec as
  published in the cassette-anatomy post and may need updating as tapes
  evolves.

## Development

```bash
cd integrations/tapes-cassette
uv sync
uv run pytest -q
uv run ruff check .
```

Tests run fully offline: tapes is mocked at the HTTP transport layer and
cognee's `add`/`cognify`/`search` are stubbed.
