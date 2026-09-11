---
name: memory
description: Use when Antigravity should remember, recall, search, improve, or forget information using Cognee.
---

# Cognee Memory

Use this skill when the user asks Antigravity to use Cognee as memory, add facts or
documents, search a knowledge graph, recall prior context, or improve existing
memory.

## Rules

- **Server first.** Every operation below goes to the running Cognee server over
  HTTP, through the plugin's own scripts. That is the authoritative path in both
  local and cloud mode.
- **`cognee-cli` is a last resort, not an alternative.** It is reachable only on a
  machine holding a cognee **source checkout**: `scripts/cognee-cli.sh` exits 64
  anywhere else, and in cloud mode the plugin's venv is never built at all. Use it
  only when the server is genuinely unreachable *and* that checkout exists. Unsure
  which mode you are in? `python3 "${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/doctor.py" --json`
  reports `mode`, `server_url` and `reachable` without importing cognee.
- **Empty CLI output is never proof of absence.** Ground-truth against the server
  before concluding anything (see *The server is the source of truth* below).
- Choose a clear dataset name with `-d` or `--dataset-name`; ask only if the dataset boundary is genuinely ambiguous.
- Do not ingest secrets, credentials, `.env` files, private keys, token dumps, or unrelated generated artifacts.
- Before destructive commands such as `forget`, `delete`, or `--everything`, get explicit user confirmation.

## Add And Build

**Server-first (one-step ingestion):**

```bash
${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-remember.sh "<text>" --node-set user_context
```

Use `--node-set project_docs` for project/code content, `--node-set agent_actions` for agent notes.

To store a **file** under its real filename (so code files ride the zero-LLM code path instead of being ingested as prose), pass `--file`:

```bash
${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-remember.sh --file src/payments.py --node-set project_docs
```

For a whole repository (cross-file calls/imports, impact analysis), use the **codebase** skill instead. The script POSTs directly to `/api/v1/remember`. A `{"ok": true}` response means the server accepted the data. An error response means the server rejected or failed the request — check `COGNEE_API_KEY` and server logs; do **not** re-run or conclude the data wasn't stored without confirming against the server.

**Background by default + eventual consistency**: the wrapper submits with `run_in_background=true` (so a large cognify never holds one request open past the cloud's ~10-min request ceiling). The POST returns once the work is **enqueued**, with `dataset_id` and `pipeline_run_id`; `status: "running"` means *submitted, not yet in the permanent graph*. The session cache is searchable immediately, but the graph is queryable only after the cognify pipeline **completes**.

By default the wrapper then waits a short, bounded time (`COGNEE_REMEMBER_WAIT_SECONDS`, default `8`) polling `/api/v1/datasets/status` and adds `"queryable": true|false` + `"wait_outcome"` to the result. `queryable: true` means it's now in the graph and an immediate recall will find it. If `queryable: false`, check `wait_outcome`: `"timeout"` means it's still processing (recall later — not an error), `"errored"` means the cognify failed (check server logs), `"unknown"` means completion couldn't be confirmed (e.g. an older server without the status route). Set `COGNEE_REMEMBER_WAIT_SECONDS=0` to skip the wait, or `COGNEE_REMEMBER_BACKGROUND=false` for a fully synchronous, immediately-queryable write (small content only — large content risks the request ceiling).

**Fallback only — server unreachable:**

```bash
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" remember <text-or-path> -d <dataset-name>
```

For staged work there is no HTTP equivalent, so it is available **only** with a
cognee source checkout. Prefer the one-step server path above; mention this
limitation rather than switching to the CLI when no checkout exists:

```bash
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" add <text-or-path> -d <dataset-name>
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" cognify -d <dataset-name>
```

For long processing:

```bash
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" remember <text-or-path> -d <dataset-name> --background
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" cognify -d <dataset-name> --background
```

## Recall And Search

**Server-first (authoritative) — use the wrapper:**

```bash
${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-search.sh "<question>" 10 --graph
```

It resolves the endpoint, the session and the API key the way the hooks do
(launch record → `COGNEE_API_KEY` → the auto-minted `api_key.json`), so it
authenticates correctly in **both** local and cloud mode. Drop `--graph` to
search the session cache and the graph, or pass `--session` for the session only.

An empty result from the server is authoritative — the server searched and found
nothing.

**Do not hand-roll the `curl` with `-H "X-Api-Key: ${COGNEE_API_KEY:-}"`.** In
local mode that variable is empty — the key is minted into
`~/.cognee-plugin/api_key.json` and never exported to your shell — so the request
401s and looks like a server problem when nothing is wrong. When you genuinely
need the raw endpoint (to pass `node_name`, say), resolve the credentials first,
in the **same** shell invocation:

```bash
eval "$(${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-forget.sh env)" && \
curl -s -X POST "${COGNEE_BASE_URL}/api/v1/recall" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${COGNEE_API_KEY}" \
  -d '{"query": "<question>", "top_k": 10, "only_context": true, "scope": ["graph"]}'
```

A `401` after that resolver means the key really is wrong for that server; a
`401` without it means nothing.

**Fallback only — server unreachable:**

```bash
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" recall "<question>" -d <dataset-name> -f pretty
```

These search modes have no HTTP equivalent, so they are available **only** with a
cognee source checkout. They are not a reason to leave the server path: if no
checkout exists, say the mode is unavailable and use the server search above.

```bash
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" search "<question>" -d <dataset-name> -t GRAPH_COMPLETION -f pretty
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" search "<exact passage or citation need>" -d <dataset-name> -t CHUNKS -k 10 -f pretty
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" search "<code question>" -d <dataset-name> -t CODE -k 10 -f pretty
```

### The server is the source of truth

`cognee-cli` is a thin client over the running Cognee server and can print **empty stdout even when content exists** (a serialization quirk). So:
- **Never conclude "not found" from an empty/clean CLI run.** Confirm against the server directly — this is authoritative.
- **Do not re-run the same CLI search to "retry."** One server answer is authoritative.
- Omit `-d <dataset>` to search **all** your datasets; restricting to one dataset can miss content that lives in another.

## Improve Memory

**Server-first (session → graph sync):**

```bash
python3 "${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/sync-session-to-graph.py"
```

**Fallback only — server unreachable:**

```bash
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" improve -d <dataset-name>
```

Bridge session feedback or Q&A into the graph:

```bash
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" improve -d <dataset-name> -s <session-id>
```

For targeted enrichment:

```bash
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" improve -d <dataset-name> --node-name <entity-name>
```

## Forget

When the user asks to forget or delete something from memory, walk the guided
flow below through the wrapper: sync the live session (so unsynced content
becomes a deletable document), find the dataset id, judge candidate documents by
their raw content — by meaning, not just keywords, and grouped by session, since
one session produces several documents carrying the same content in different
forms — confirm with the user, then delete each match:

```bash
${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-forget.sh sync
${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-forget.sh datasets
${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-forget.sh data <dataset_id>
${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-forget.sh raw <dataset_id> <data_id>
${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-forget.sh forget <dataset_id> <data_id>
```

The wrapper always authenticates (env → `~/.cognee/.env` → the auto-minted
local `api_key.json`) and prints an `HTTP <status>` trailer per call. Deletion
is irreversible — use the narrowest scope possible and confirm first.

**Fallback only — server unreachable:**

```bash
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" forget --dataset <dataset-name> --data-id <data-uuid>
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" forget --dataset <dataset-name>
```

Avoid the CLI's `forget --everything` unless the user explicitly asks to delete
all Cognee data.
