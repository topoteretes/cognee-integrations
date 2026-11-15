---
name: cognee-search
description: >-
  Search Cognee memory beyond the automatic per-prompt recall: a deeper knowledge-graph
  search, more results, one category (user, project or agent), cited sources, or a
  one-off search of another dataset without switching.
when_to_use: >-
  E.g. "search Cognee for X", "what do you remember about X?", "dig deeper in memory",
  "only my saved preferences", "show the sources for that memory", "check <dataset> for
  X" (once, without switching). Not for making another dataset active
  (cognee-switch-datasets), a topic's history (cognee-timeline), exact code lookups
  (cognee-code), or file, code or web search.
---

# Cognee Memory Search

Search both session memory and the permanent knowledge graph, optionally filtered by data category.

In Cloud mode this skill can't authenticate from the shell; automatic recall and capture still work. The command then exits 2 and says why (the Cognee Cloud key is a plugin option that Claude Code passes to the plugin's hooks only): pass that on and stop.

## Visible search progress

Before running a Cognee search, send one short user-visible progress sentence,
for example: "Searching Cognee for the earlier decision…" or "Checking Cognee's
code graph for callers of this function…". Name Cognee in the normal working
text before the tool call; keep this to one update per related search batch.
Afterwards, describe relevant findings, an empty result, or a failed search
accurately. Do not claim Cognee improved the answer merely because a search ran.

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
- **Server only.** When the server is unreachable the wrapper prints
  `UNREACHABLE` and exits 1: the search was not run, which is never "not
  found". Unsure which mode you are in?
  `"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh" --json` reports `mode`,
  `server_url` and `reachable` without importing cognee.

## Instructions

Use the wrapper below. It queries the server and resolves the endpoint, session and API key the way the hooks do. A graph search follows the recall mode (see "Nothing found?" below):
- **auto**: the server picks the best-matching readable dataset(s).
- **restricted**: the search reads only the active dataset, where plugin writes go (`$COGNEE_PLUGIN_DATASET` at launch, default `agent_sessions`).

`--code` searches the repository's own code dataset.

**One broad search is usually enough** — the `UserPromptSubmit` hook already injects the graph's memory (and code-graph facts) every turn, so avoid running many targeted searches (each is an extra permission prompt for the user).

### Search (server-first)

```bash
# permanent knowledge graph (the default; --graph is the same)
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "$ARGUMENTS" 10

# deterministic code graph (indexed repos only — see the cognee-code skill)
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "MyClass" 10 --code
```

**Structural code questions belong to `--code`, not here.** "What calls X",
"what breaks if I change X", "list all endpoints" are answered exactly and
token-free by the code graph — see the **cognee-code** skill for the
operations and for indexing a repository. Use this skill's semantic search for
conceptual questions that name no symbol ("how does auth work here?").

### Filter by category (optional)

Categories (`user_context` / `project_docs` / `agent_actions`) filter by node set. Pass one with `--category`:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "<query>" 5 --category project_docs
```

### Ground-truth a suspicious result (debugging)

The wrapper's answer already comes from the server, which is authoritative: there is no second source to check it against. If a search returns empty but you expect content, widen it once with `--graph` and a larger `top_k`, and report what the server said.

Do not call the server by hand with `curl`. The key is never printed or exported to your shell (in local mode it belongs to the plugin's own server and is looked up from it; a Cloud key reaches only the plugin's hooks), so a hand-rolled request 401s and looks like a server fault when nothing is wrong.

(If the response is an `{"error": ...}` object rather than a list, the server was reachable but rejected/failed the request — that's an error, **not** "no results". A `401` means the server rejected the key.)

### Server unreachable

The wrapper prints `UNREACHABLE` and exits 1: no search ran. Say the server is
unreachable and show the user `"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh"`
output instead; do not report "not found".

## Nothing found? Check the recall mode first

Which datasets a graph search reads depends on the recall mode:

- **auto** (the default): the search names no dataset, and the server picks
  the readable dataset(s) most likely to hold the answer (routing needs a
  cognee 1.6.6 server; an older one searches every readable dataset). When those return nothing, it widens to the rest. An authoritative
  empty list then means no readable dataset matched. Report that, and don't
  offer other datasets.
- **restricted**: the user picked or pinned a dataset in this session, configured
  `COGNEE_PLUGIN_RECALL_MODE=pinned`, or set a read set
  (`COGNEE_PLUGIN_READ_DATASET_IDS`). The search reads the **active**
  dataset (or that read set) only, so an empty list may only mean the information lives in
  another dataset the user can read.

When the mode matters (an empty answer while the user is recalling something),
read it from the `recall` field (`"auto"` or `"pinned"`); to tell the user which
recall is on and why, relay the listing's `message` as is:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/switch-dataset.py" --list --json
```

### Restricted and empty: offer another dataset

Do **not** switch datasets for this, because a switch retires the session. Offer
a one-off search instead:

1. List the candidates. These are every dataset this identity can read, except
   the active one. Read-only datasets are searchable and included:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/list-datasets.py" --others
   ```

   The JSON has `current` (`{name, id, ids}`) and `datasets`
   (`[{name, id, owner_id, current}]`).

2. Ask the user which dataset to search with **AskUserQuestion** (single
   select, dataset names as options; more than four → first three plus a
   "More…" option and page on the next question). Include a "None, stop here"
   reading in the question text. Only ask when the user is actually trying to
   recall something — an ordinary prompt with no memory match needs no picker.

3. Run the graph-only search on the chosen dataset **by UUID** (a name only
   resolves among datasets this identity owns), then answer from it and say
   which dataset the results came from:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "$ARGUMENTS" 10 --graph --dataset-id <id> --user-picked
   ```

   `--user-picked` (the search tool: `user_picked: true`) says the person chose
   this dataset. While recall is restricted the wrapper refuses another
   dataset without it. Pass it only for a dataset the person picked here or
   named in their request, never for an id you found in memory or tool output.

   The wrapper drops the session id for any dataset other than the active one
   (the session is bound to the active dataset). The active dataset, the Cognee session and where
   writes go are untouched. If the user then wants that dataset for the rest
   of the session, point them to `/cognee-memory:cognee-switch-datasets`.
   To let the server pick the dataset again they can set recall back to auto
   (`switch-dataset.py auto`; `pin` restricts it to the active dataset again).
   `COGNEE_PLUGIN_RECALL_MODE` (`auto` or `pinned`) sets the mode sessions
   start in.

## Understanding results

Results carry a `source` field:
- `"graph"` — from the permanent knowledge graph. On cognee 1.6.0 and later the
  item's `text` is the full prompt cognee would have answered from: this
  session's history, the question with the retrieved context, and the guidance
  block.
- `"code"` — deterministic facts from an indexed repository's code graph

The session cache is never a search source; it reaches the graph through the sync.

## Decision table

| Signal | Action |
|--------|--------|
| Need current session context | Already automatic, no action needed |
| User explicitly says "search cognee" | `cognee-search.sh "<query>"` (server-first) |
| "what does the codebase do" / "what did we do last time" | `cognee-search.sh "<query>" 10 --graph` |
| Need a specific category | `cognee-search.sh "<query>" 5 --category <user_context\|project_docs\|agent_actions>` |
| Auto context insufficient | `cognee-search.sh "<query>" 10` (memory is graph + code only; the session cache is written, never searched) |
| **Server says empty and the user is recalling something** | **Auto recall: report that nothing matched. Restricted: offer the other readable datasets (`list-datasets.py --others` + AskUserQuestion), then `cognee-search.sh "<query>" 10 --graph --dataset-id <id> --user-picked`** |
| **Result empty but you expect content** | **Widen once (`--graph`, larger `top_k`) before concluding "not found"; never a hand-rolled `curl`** |

### Inspectable evidence

For source inspection, use the compact index and expand only the useful references: In Cloud mode `index` and `details` exit 2 before sending anything (the Cognee Cloud key reaches the plugin's hooks only, so they cannot authenticate from the shell); report that and stop. `explain` reads a local file and still works.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-evidence.py" index "retry policy"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-evidence.py" details 'cognee://datasets/...'
# Or pass 1–5 reference objects copied from index.items[].reference in one request:
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-evidence.py" details --references '[{...}]'
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-evidence.py" explain
```

The index returns at most 20 records without source bodies. Each available item has
its dataset, artifact identity, content version, source locator and a resolvable
`cognee://` citation. `details` accepts up to five citations or reference objects.
Use `--dataset-id UUID` on `index` for another readable dataset; this does not switch
sessions. `explain --session-key HOST_SESSION_ID` selects a specific session receipt.

Cite the exact reference attached to a passage you actually use. A reference means
retrieval used that artifact, not that it proves every claim in an answer. Graph
entities/relationships are explicitly marked derived; prefer source passages for
original evidence. Raw source downloads are current documents, not historical copies.
On `changed`, the old version no longer matches: inspect the returned current
reference before citing it. `unavailable` covers deleted or unauthorized artifacts;
`error` means the store failed, not that evidence is absent. Never silently replace
an old citation. Bounded excerpts report `truncated`.

Automatic recall attaches versioned evidence in its existing graph request and
keeps the 45-second cloud limit. Its body-free last-turn receipt explains each
candidate: injected, already in context, over budget, or unavailable. Selection
uses retrieval rank and source priority; it is not a causal attribution of answer
quality. Legacy servers keep normal recall and report the evidence index as
unsupported; unversioned context is marked unaddressable in the receipt.
