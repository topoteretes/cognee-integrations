# Papercomputes Tapes to Cognee Exporter

A proof-of-concept sync pipeline that pulls agent session data from [Tapes](https://tapes.dev) into [Cognee](https://cognee.ai) for knowledge graph construction.

## What it does

1. Fetches sessions from a running Tapes instance via its HTTP API
2. Filters to completed sessions, extracts the "main" conversational turns (skipping injected system context, permission checks, and other harness-internal offshoots)
3. Builds a readable transcript per session, including compact tool-use detail
4. Ingests each session into Cognee via `add()` + `cognify()`
5. Tracks progress with a content-hash manifest for idempotency, and a timestamp checkpoint for incremental syncs

## Setup

Requires:
- A running Tapes instance (`tapes local up && tapes serve`)
- Python 3.12+, `uv`-managed virtualenv
- An OpenAI API key (used for Cognee's embeddings) set as `OPENAI_API_KEY`

From a checkout of this repository:
 
```bash
cd integrations/tapes
pip install -e .
```

## Configuration reference
 
| Setting | Env var | Default |
|---|---|---|
| Tapes API base URL | `TAPES_BASE_URL` | `http://localhost:8081` |
| Dataset name | `TAPES_DATASET` | `tapes_sessions` |
| Manifest path | `MANIFEST_PATH` | `.cognee-manifest-<dataset-name>.json` |
| Graph output | `GRAPH_OUTPUT` | `graph.html` |
| Log level | `LOG_LEVEL` | `INFO` |

## Running

```bash
cognee-tapes-sync
```
 
or, without installing the console script:
 
```bash
python3 -m cognee_integration_tapes.tapes_import
```

On first run, this fetches and ingests all completed sessions. Subsequent runs only fetch sessions newer than the last successful sync (tracked in `manifest.json`, which is gitignored — local state only).
Each run generates a `graph.html`, can be viewed in browser.

## ⚠️ Needs verification before relying on incremental sync
 
`get_last_seen_at()` reads a field whose location in the `/export` payload was never actually confirmed against a real response — every `/export` call made during development returned a 404 (only demo/seed sessions were available locally, and export appears broken for those specifically). The code checks two plausible locations and falls back between them, but this is an untested guess, not a confirmed contract.
 
If neither location has the field, the code logs a warning rather than failing silently. The underlying uncertainty is still there. **Before merging or relying on incremental sync in production, someone with a real Tapes deployment should confirm this against an actual exported session.** Worst case if it's wrong: every run re-fetches full history instead of just what's new (wasteful, not incorrect, since the manifest hash still prevents re-ingesting unchanged sessions).

## Known limitations
 
- This script fetches all session IDs via `/v1/sessions` (paginated) and filters client-side by `last_seen_at`.
- Occasional benign `Unclosed client session` warnings from Cognee's SDK on heavier operations (large `cognify()` runs, `visualize_graph()`) — cosmetic, doesn't affect correctness of ingested data.
- Tool-use detail surfaces a curated set of argument keys (command, file paths, subagent prompts, task metadata, etc.) rather than full raw tool inputs, to avoid bloating the graph with large diffs/file contents.
- **`last_seen_at`'s exact location in the `/export` payload is unconfirmed** — the code checks both the nested `session.last_seen_at` location and a top-level fallback, so it works either way, but this hasn't been verified against a live non-demo session (only demo/seed sessions were available while building this). Worth confirming against real session data before relying on incremental sync in production; worst case if wrong is a full re-fetch each run, not incorrect ingestion (the manifest hash still blocks re-adding unchanged sessions).
- **No authentication on Tapes API calls** — this assumes a local, trusted, unauthenticated Tapes server. If pointed at a non-local or multi-tenant Tapes deployment, auth would need to be added.
- **Manifest locking is POSIX-only** (uses `fcntl`). Works on macOS/Linux; would need `filelock` or similar for Windows support.
- **No retry/backoff on transient HTTP failures** — a failed session fetch or a failed page of the session list is logged and skipped/truncated rather than retried. Fine for a POC, worth adding (e.g. `tenacity` or manual backoff) if this runs unattended on a schedule.
- **Sequential HTTP requests, no connection reuse** — each session fetch opens a fresh connection rather than reusing a `requests.Session()`. Not a correctness issue, just slower than necessary for vaults/session counts in the hundreds+.

## Development (N/a yet)
 
```bash
cd integrations/tapes
pip install -e ".[dev]"
pytest -q
```
