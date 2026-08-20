---
name: memory
description: Use when Codex should remember, recall, search, improve, or forget information using Cognee.
---

# Cognee Memory

Use this skill when the user asks Codex to use Cognee as memory, add facts or
documents, search a knowledge graph, recall prior context, or improve existing
memory.

## Rules

- Prefer the server-first paths below (HTTP to the running Cognee server).
- Use `uv run cognee-cli ...` only when the server is genuinely unreachable.
- Choose a clear dataset name with `-d` or `--dataset-name`; ask only if the dataset boundary is genuinely ambiguous.
- Do not ingest secrets, credentials, `.env` files, private keys, token dumps, or unrelated generated artifacts.
- Before destructive commands such as `forget`, `delete`, or `--everything`, get explicit user confirmation.

## Add And Build

**Server-first (one-step ingestion):**

```bash
${CODEX_PLUGIN_ROOT}/scripts/cognee-remember.sh "<text>" --node-set user_context
```

Use `--node-set project_docs` for project/code content, `--node-set agent_actions` for agent notes. The script POSTs directly to `/api/v1/remember`. A `{"ok": true}` response means the server accepted the data. An error response means the server rejected or failed the request — check `COGNEE_API_KEY` and server logs; do **not** re-run or conclude the data wasn't stored without confirming against the server.

**Background by default + eventual consistency**: the wrapper submits with `run_in_background=true` (so a large cognify never holds one request open past the cloud's ~10-min request ceiling). The POST returns once the work is **enqueued**, with `dataset_id` and `pipeline_run_id`; `status: "running"` means *submitted, not yet in the permanent graph*. The session cache is searchable immediately, but the graph is queryable only after the cognify pipeline **completes**.

By default the wrapper then waits a short, bounded time (`COGNEE_REMEMBER_WAIT_SECONDS`, default `8`) polling `/api/v1/datasets/status` and adds `"queryable": true|false` + `"wait_outcome"` to the result. `queryable: true` means it's now in the graph and an immediate recall will find it. If `queryable: false`, check `wait_outcome`: `"timeout"` means it's still processing (recall later — not an error), `"errored"` means the cognify failed (check server logs), `"unknown"` means completion couldn't be confirmed (e.g. an older server without the status route). Set `COGNEE_REMEMBER_WAIT_SECONDS=0` to skip the wait, or `COGNEE_REMEMBER_BACKGROUND=false` for a fully synchronous, immediately-queryable write (small content only — large content risks the request ceiling).

**Fallback only — server unreachable:**

```bash
uv run cognee-cli remember <text-or-path> -d <dataset-name>
```

For staged work (no HTTP equivalent — CLI only):

```bash
uv run cognee-cli add <text-or-path> -d <dataset-name>
uv run cognee-cli cognify -d <dataset-name>
```

For long processing:

```bash
uv run cognee-cli remember <text-or-path> -d <dataset-name> --background
uv run cognee-cli cognify -d <dataset-name> --background
```

## Recall And Search

**Server-first (authoritative):**

```bash
curl -s -X POST "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/recall" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}" \
  -d '{"query": "<question>", "top_k": 10, "only_context": true, "scope": ["graph"]}'
```

Omit `-H "X-Api-Key: ..."` for a local single-user server (auth is optional). An empty list `[]` from the server is authoritative — the server searched and found nothing.

**Fallback only — server unreachable:**

```bash
uv run cognee-cli recall "<question>" -d <dataset-name> -f pretty
```

Search modes (CLI only):

```bash
uv run cognee-cli search "<question>" -d <dataset-name> -t GRAPH_COMPLETION -f pretty
uv run cognee-cli search "<exact passage or citation need>" -d <dataset-name> -t CHUNKS -k 10 -f pretty
uv run cognee-cli search "<code question>" -d <dataset-name> -t CODE -k 10 -f pretty
```

### The server is the source of truth

`cognee-cli` is a thin client over the running Cognee server and can print **empty stdout even when content exists** (a serialization quirk). So:
- **Never conclude "not found" from an empty/clean CLI run.** Confirm against the server directly — this is authoritative.
- **Do not re-run the same CLI search to "retry."** One server answer is authoritative.
- Omit `-d <dataset>` to search **all** your datasets; restricting to one dataset can miss content that lives in another.

## Improve Memory

**Server-first (session → graph sync):**

```bash
python3 "${CODEX_PLUGIN_ROOT}/scripts/sync-session-to-graph.py"
```

**Fallback only — server unreachable:**

```bash
uv run cognee-cli improve -d <dataset-name>
```

Bridge session feedback or Q&A into the graph:

```bash
uv run cognee-cli improve -d <dataset-name> -s <session-id>
```

For targeted enrichment:

```bash
uv run cognee-cli improve -d <dataset-name> --node-name <entity-name>
```

## Forget

When the user asks to forget or delete something from memory, follow the
**cognee-forget** skill — it walks the full guided flow: sync the live session,
find the dataset id, judge candidate documents by raw content (grouped by
session), confirm, then delete each match through the wrapper:

```bash
${CODEX_PLUGIN_ROOT}/scripts/cognee-forget.sh sync
${CODEX_PLUGIN_ROOT}/scripts/cognee-forget.sh datasets
${CODEX_PLUGIN_ROOT}/scripts/cognee-forget.sh data <dataset_id>
${CODEX_PLUGIN_ROOT}/scripts/cognee-forget.sh raw <dataset_id> <data_id>
${CODEX_PLUGIN_ROOT}/scripts/cognee-forget.sh forget <dataset_id> <data_id>
```

The wrapper always authenticates (env → `~/.cognee/.env` → the auto-minted
local `api_key.json`) and prints an `HTTP <status>` trailer per call. Deletion
is irreversible — use the narrowest scope possible and confirm first.

**Fallback only — server unreachable:**

```bash
uv run cognee-cli forget --dataset <dataset-name> --data-id <data-uuid>
uv run cognee-cli forget --dataset <dataset-name>
```

Avoid `uv run cognee-cli forget --everything` unless the user explicitly asks
to delete all Cognee data.
