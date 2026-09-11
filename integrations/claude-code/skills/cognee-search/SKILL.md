---
name: cognee-search
description: Search Cognee memory. Session memory is automatically searched on every prompt via hooks. Use this skill explicitly for permanent knowledge graph search, filtered category search, or when you need more results than the automatic lookup provides.
---

# Cognee Memory Search

Search both session memory and the permanent knowledge graph, optionally filtered by data category.

## Automatic session search

Session memory is searched **automatically on every user prompt** via the `UserPromptSubmit` hook. You do not need to run this skill to access current-session context.

## Data categories

Knowledge is organized into three categories via `node_set`:

| Category | Node set | Contains |
|----------|----------|----------|
| **user** | `user_context` | User preferences, corrections, personal facts |
| **project** | `project_docs` | Repository docs, code context, architecture decisions |
| **agent** | `agent_actions` | Tool call logs, reasoning traces, generated artifacts |

## Rules

- **Server first.** Search goes through the **running Cognee server**
  (`POST /api/v1/recall`) via the wrapper below — the source of truth in both
  local and cloud mode.
- **`cognee-cli` is a last resort, not an alternative.** It is reachable only on a
  machine holding a cognee **source checkout**, and in cloud mode the plugin's
  venv is never built at all. Use it only when the server is genuinely
  unreachable *and* that checkout exists. Unsure which mode you are in?
  `${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh --json` reports `mode`,
  `server_url` and `reachable` without importing cognee.
- **Empty CLI output is never proof of absence.** Ground-truth against the server
  before concluding anything.

## Instructions

Use the wrapper below: it queries the server, scoped to the **plugin's dataset** (`$COGNEE_PLUGIN_DATASET`, default `agent_sessions` — the same dataset all plugin writes target, so unrelated datasets don't bleed in), and resolves the endpoint, session and API key the way the hooks do.

**One broad search is usually enough** — the `UserPromptSubmit` hook already injects session/trace/graph context every turn, so avoid running many targeted searches (each is an extra permission prompt for the user).

### Search (server-first)

```bash
# session cache + permanent graph (default)
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh "$ARGUMENTS"

# permanent graph only
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh "$ARGUMENTS" 10 --graph

# current session only
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh "$ARGUMENTS" 10 --session

# deterministic code graph (indexed repos only — see the cognee-code skill)
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh "MyClass" 10 --code
```

**Structural code questions belong to `--code`, not here.** "What calls X",
"what breaks if I change X", "list all endpoints" are answered exactly and
token-free by the code graph — see the **cognee-code** skill for the
operations and for indexing a repository. Use this skill's semantic search for
conceptual questions that name no symbol ("how does auth work here?").

### Filter by category (optional)

Categories (`user_context` / `project_docs` / `agent_actions`) filter by node set. The wrapper does not expose this — pass `node_name` to the server directly.

**Resolve credentials first.** In local mode `$COGNEE_API_KEY` is empty (the key is minted into `~/.cognee-plugin/api_key.json` and never exported to your shell), so a hand-rolled `curl` with `-H "X-Api-Key: ${COGNEE_API_KEY:-}"` 401s and looks like a server fault when nothing is wrong. Use the forget helper's resolver in the **same** shell invocation — exports do not persist across separate Bash calls:

```bash
eval "$(${CLAUDE_PLUGIN_ROOT}/scripts/cognee-forget.sh env)" && \
curl -s -X POST "${COGNEE_BASE_URL}/api/v1/recall" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${COGNEE_API_KEY}" \
  -d '{"query": "...", "top_k": 5, "only_context": true, "scope": ["graph"], "node_name": ["project_docs"], "datasets": ["'"${COGNEE_PLUGIN_DATASET:-agent_sessions}"'"]}'
```

### Ground-truth a suspicious result (debugging)

The server is authoritative. If a search returns empty but you expect content, confirm directly — **do not** conclude "not found" from empty CLI output. Resolve credentials in the same invocation, as above:

```bash
eval "$(${CLAUDE_PLUGIN_ROOT}/scripts/cognee-forget.sh env)" && \
curl -s -X POST "${COGNEE_BASE_URL}/api/v1/recall" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${COGNEE_API_KEY}" \
  -d '{"query": "...", "top_k": 5, "only_context": true, "scope": ["graph"], "datasets": ["'"${COGNEE_PLUGIN_DATASET:-agent_sessions}"'"]}'
```

(The server enforces auth in both modes, which is why the resolver is not optional. If the response is an `{"error": ...}` object rather than a list, the server was reachable but rejected/failed the request — that's an error, **not** "no results". A `401` *after* the resolver means the key really is wrong for that server; a `401` without it means nothing.)

### Fallback only — server unreachable

`cognee-cli` is a thin client over the same server and can print **empty stdout even when content exists**. It also requires a cognee source checkout, so on a normal install it is simply unavailable — an absent CLI is not a Cognee fault to report. Use it only when the server is down *and* a checkout exists, and treat empty output as *inconclusive*, never as "no results":

```bash
cognee-cli recall "$ARGUMENTS" -k 5 -f json
```

If the CLI is missing, say the server is unreachable and show the user
`${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh` output instead.

## Understanding results

Results include a `_source` field:
- `"session"` — from the session cache (current conversation)
- `"graph"` — from the permanent knowledge graph
- `"code"` — deterministic facts from an indexed repository's code graph

Session entries tagged with `[category:agent]` are automatic tool call logs.

## Decision table

| Signal | Action |
|--------|--------|
| Need current session context | Already automatic, no action needed |
| User explicitly says "search cognee" | `cognee-search.sh "<query>"` (server-first) |
| "what does the codebase do" / "what did we do last time" | `cognee-search.sh "<query>" 10 --graph` |
| Need a specific category | use the `node_name` curl form above (`["user_context"\|"project_docs"\|"agent_actions"]`) |
| Auto context insufficient | `cognee-search.sh "<query>" 10 --session` |
| **Result empty but you expect content** | **Ground-truth via the credential-resolving `curl` above before concluding "not found"** |
