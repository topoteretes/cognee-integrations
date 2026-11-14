---
name: cognee-remember
description: >-
  Store a fact, preference, decision, note or file permanently in the Cognee knowledge
  graph, tagged as user, project or agent. Plain "remember this" requests go here, not
  to Claude's built-in memory.
when_to_use: >-
  E.g. "remember that I prefer X", "save this to memory", "keep this in mind for next
  time", "don't forget that X", "store this file in memory". Not for a wrap-up note with
  next steps (cognee-handoff), the whole session (cognee-sync), indexing a repo
  (cognee-code), reminders for the current task, or when CLAUDE.md, MEMORY.md or /memory
  is named.
---

# Cognee Permanent Memory Storage

Store data permanently in the Cognee knowledge graph with category tagging.

In Cloud mode this skill can't authenticate from the shell; automatic recall and capture still work. The command then exits 2 and says why (the Cognee Cloud key is a plugin option that Claude Code passes to the plugin's hooks only): pass that on and stop.

## Rules

- **Server first.** Writes go to the running Cognee server over HTTP through the
  wrapper below — the authoritative path in both local and cloud mode.
- **Server only.** When the server is unreachable the wrapper prints
  `UNREACHABLE` and exits 1: nothing was saved. Unsure which mode you are in?
  `"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh" --json` reports `mode`,
  `server_url` and `reachable` without importing cognee.

## Data categories

Cognee organizes knowledge into three categories via `node_set` tagging:

| Category | Node set | What belongs here |
|----------|----------|-------------------|
| **user** | `user_context` | User preferences, corrections, personal facts, communication style |
| **project** | `project_docs` | Repository docs, code context, architecture decisions, company data |
| **agent** | `agent_actions` | Tool call logs, reasoning traces, generated artifacts (auto-captured by hooks) |

## Instructions

Determine the category from the user's intent, then run:

**User data** (preferences, corrections, personal context):
```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-remember.sh" "$ARGUMENTS" --node-set user_context
```

**Project data** (docs, code, company knowledge):
```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-remember.sh" "$ARGUMENTS" --node-set project_docs
```

**Agent data** (explicit agent notes — routine tool logs are automatic):
```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-remember.sh" "$ARGUMENTS" --node-set agent_actions
```

**Storing a file (code included)**: pass `--file` so the upload keeps its real
filename — the extension is the server's routing signal, and a code file
(`.py`/`.ts`/`.go`/...) then rides the zero-LLM code path instead of being
ingested as prose:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-remember.sh" --file src/payments.py --node-set project_docs
```

For a whole repository (cross-file calls/imports, impact analysis), index it
instead — see the **cognee-code** skill.

The wrapper POSTs to the running Cognee server (`/api/v1/remember`). A `{"ok": true}` response means the server accepted the data. An error response means the server rejected or failed the request (on HTTP 401/403, the server rejected the key) — check the server logs; do **not** re-run or conclude the data wasn't stored without confirming against the server.

**Background by default + eventual consistency**: the wrapper submits with `run_in_background=true` (so a large cognify never holds one request open past the cloud's ~10-min request ceiling). The POST returns once the work is **enqueued**, with `dataset_id` and `pipeline_run_id`; `status: "running"` means *submitted, not yet in the permanent graph*. The graph is queryable only after the cognify pipeline **completes**; until then a search will not find the new content.

By default the wrapper then waits a short, bounded time (`COGNEE_REMEMBER_WAIT_SECONDS`, default `8`) polling `/api/v1/datasets/status` and adds `"queryable": true|false` + `"wait_outcome"` to the result. `queryable: true` means it's now in the graph and an immediate recall will find it. If `queryable: false`, check `wait_outcome`: `"timeout"` means it's still processing (recall later — not an error), `"errored"` means the cognify failed (check server logs), `"unknown"` means completion couldn't be confirmed (e.g. an older server without the status route). Set `COGNEE_REMEMBER_WAIT_SECONDS=0` to skip the wait, or `COGNEE_REMEMBER_BACKGROUND=false` for a fully synchronous, immediately-queryable write (small content only — large content risks the request ceiling).

## Server unreachable

The wrapper prints `UNREACHABLE` and exits 1: the content was **not** saved.
Tell the user it wasn't saved and to retry once the server is back, and show
`"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh"` output — do not report the
memory as saved.

## When to use

- User says "remember this" or "save this" → category **user**
- User says "remember this about the project/codebase" → category **project**
- You want to persist your own findings or conclusions → category **agent**
- NOT for routine tool call logging (that's automatic via hooks with `agent_actions` tagging)

## Category routing guide

| Signal | Category |
|--------|----------|
| "remember my preference for..." | user |
| "I always want..." / "I prefer..." | user |
| "remember this about the codebase" | project |
| "save these docs" / "index this file" | project |
| "note that this API works like..." | project |
| "remember what we discovered" | agent |
| Routine tool calls | agent (automatic, no action needed) |
