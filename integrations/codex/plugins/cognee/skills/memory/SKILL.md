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

Use `--node-set project_docs` for project/code content, `--node-set agent_actions` for agent notes. The script POSTs directly to `/api/v1/remember` and returns `{"ok": true}` on success.

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

Omit `-H "X-Api-Key: ..."` for a local single-user server (auth is optional). An empty list `[]` from the server is authoritative — the server searched and found nothing. One exception: if the plugin's search wrapper returns an object with `captured_pending`, content captured this session is still waiting on cognify — retry shortly or run `python3 "${CODEX_PLUGIN_ROOT}/scripts/sync-session-to-graph.py"` instead of treating it as "no memory" or grepping the transcript.

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

Use the narrowest deletion command possible and confirm first:

```bash
uv run cognee-cli forget --dataset <dataset-name>
uv run cognee-cli forget --dataset <dataset-name> --data-id <data-uuid>
```

Avoid `uv run cognee-cli forget --everything` unless the user explicitly asks
to delete all Cognee data.
