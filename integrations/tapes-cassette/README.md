# Cognee cassette for Paper's tapes

[Cognee](https://cognee.ai) memory as a first-class [tapes](https://tapes.dev)
**cassette**: an independent HTTP service that tapes discovers, validates, and
proxies under its own API namespace, following the
[`cassette/v1alpha1` contract](https://papercompute.com/blog/cassette-anatomy/).

It syncs recorded agent sessions from the tapes core API into a cognee
knowledge graph and answers questions over them — and exposes both operations
as MCP tools inside tapes, so agents can use their own session history as
memory.

## How it differs from the `tapes` exporter integration

The [`integrations/tapes`](../tapes) exporter (PR #362) is a standalone CLI
that *polls* tapes from the outside. This cassette integrates *inside* the
tapes namespace instead:

| | Exporter (`integrations/tapes`) | Cassette (this package) |
|---|---|---|
| Shape | One-shot CLI sync script | Long-running service, discovered by tapes |
| API surface | None | `/v1/cassettes/cognee/...` (proxied by tapes) |
| Agent access | None | MCP tools: `cognee.sync_sessions`, `cognee.sync_status`, `cognee.search_memory` |
| Config | Env vars only | Env vars, declared in the `x-tapes-cassette` manifest |
| Checkpoint source | `last_seen_at` from the **export payload** (location unverified) | `last_seen_at` from the **session list items** (confirmed field) |

The transcript extraction rules are shared with the exporter: only completed
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

The exporter PR left `last_seen_at`'s location in the `/export` payload
unverified (every `/export` call 404'd during its development). This cassette
sidesteps that entirely: the checkpoint is computed from `last_seen_at` on
`GET /v1/sessions` **list items**, which the list endpoint is known to return.
Only completed sessions advance the checkpoint — an in-progress session's
`last_seen_at` bumps again when it completes, so the next incremental run
picks it up.

## Setup

Requires Python 3.10+, a running tapes instance, and an OpenAI API key for
cognee's in-process embeddings/LLM calls.

```bash
cd integrations/tapes-cassette
pip install -e .
cp .env.example .env   # fill in OPENAI_API_KEY
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

## Configuration

| Setting | Env var | Default |
|---|---|---|
| Tapes core API base URL | `TAPES_BASE_URL` | `http://127.0.0.1:8081` |
| Cognee dataset name | `COGNEE_TAPES_DATASET` | `tapes_sessions` |
| Cassette listen host/port | `CASSETTE_HOST` / `CASSETTE_PORT` | `127.0.0.1` / `9900` |
| Sync state file | `CASSETTE_STATE_PATH` | `.cognee-cassette-state-<dataset>.json` |
| Forced cognee storage root | `COGNEE_STORAGE_ROOT` | unset (cognee defaults) |
| Log level | `LOG_LEVEL` | `INFO` |

Set `COGNEE_STORAGE_ROOT` to keep the cassette's cognee data/system storage
isolated under one directory — recommended if your shell exports global cognee
storage variables.

The same settings are declared (with types and defaults) in the manifest's
`x-tapes-cassette.config` block, so tapes can introspect them.

## Known limitations

- **No authentication on tapes core API calls** — assumes a local, trusted
  tapes deployment, same as the exporter.
- **Single-process state** — sync state lives in a local JSON file guarded by
  the server's single event loop; don't run two cassette instances against the
  same state file.
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
