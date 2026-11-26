---
name: cognee-code
description: >-
  Index a repository (local path or git URL) into Cognee's code graph and answer exact
  structural queries with no LLM calls: callers, dependencies, impact of changing a
  symbol, call paths, endpoints, re-index changes.
when_to_use: >-
  E.g. "index this repo", "what calls or uses X?", "what breaks if I change X?", "how
  does A reach B?", "list all API endpoints", "what did the last re-index change?". Not
  for overviews, hotspots, diagrams or a whole change's impact (cognee-architecture),
  conceptual questions naming no symbol (cognee-search), storing one file
  (cognee-remember), or ordinary edits or debugging.
---

# Cognee Code Graph

Build and query an architectural graph of a codebase (symbols, calls, imports,
endpoints, dependencies) via Cognee's enola-backed pipeline. Requires a Cognee
server >= 1.5.3. Indexing makes **no LLM or embedding calls** — it is fast,
deterministic, and token-free.


## Visible search progress

Before running a Cognee search, send one short user-visible progress sentence,
for example: "Searching Cognee for the earlier decision…" or "Checking Cognee's
code graph for callers of this function…". Name Cognee in the normal working
text before the tool call; keep this to one update per related search batch.
Afterwards, describe relevant findings, an empty result, or a failed search
accurately. Do not claim Cognee improved the answer merely because a search ran.

## Index a repository

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-index-repo.sh" <repo-path-or-git-url> [--dataset <name>] [--index-vectors] [--wait <seconds>]
```

- **Local path** (e.g. `.` or `/path/to/repo`): a local server reads it in
  place. A cloud or other remote server cannot read this disk, so the command
  sends it the checkout's git remote as an `https` URL instead and the server
  clones that (no file is uploaded, no local credential is sent). A checkout
  with no git remote cannot be indexed there, and the server must be able to
  clone the URL without credentials — a private repository fails.
- **Git URL** (`https://github.com/org/repo`): the server shallow-clones it.
  Freshness follows *pushed* commits — local edits are invisible to it.
- Default dataset is `codebase-<repo-name>-<digest>`, one per indexed repo —
  narrow datasets keep code searches fast, and the path digest keeps two
  checkouts that share a basename (`~/work/a/service`, `~/work/b/service`) from
  landing in one graph. The name is printed on success; you rarely need it,
  because `--code` searches resolve it from the current checkout.
- `--index-vectors`: also embeds the code facts so semantic/hybrid search can
  see them (needs an embedding provider). Without it, code knowledge is
  reachable ONLY through code search below — conceptual questions about the
  code then rely on ingested docs/READMEs.
- The submit returns quickly (background pipeline). `--wait 60` polls until
  the graph is queryable. Poll manually via
  `GET /api/v1/datasets/status?dataset=<id>&pipeline=code_graph_pipeline`.

Indexing writes enola's snapshot to `<repo>/.enola/` (untracked) — add it to
`.gitignore` or global excludes. The plugin's change detection ignores it.

**Indexing is also the freshness opt-in**: for local-path repos the plugin
records a git fingerprint and automatically re-submits the repo in the
background when a turn changed the working tree, so the graph tracks your
edits. Re-indexing an unchanged repo is skipped server-side (content hashes),
so re-running the command is always safe.

## Indexing at session start: the user decides

A repository is never indexed on the plugin's own initiative. When a session
opens inside a git repository that holds code and is not indexed yet, the
session-start context tells you to ask the user once, with their first message
(AskUserQuestion), and gives the command for each answer. Follow it as written:

| Answer | What you do |
|--------|-------------|
| Index it | Run the index command the context gives (`cognee-index-repo.sh <repo> --wait 60`) and report the outcome in one line |
| Not now | Nothing. A later session asks again |
| Never for this repository | Run `cognee-index-repo.sh <repo> --decline`. It is not asked again; "index this repo" still works |

Never index without a yes, and do not ask twice in one session. What the yes
means depends on the server, and the question says so:

| Situation | Behavior |
|-----------|----------|
| Local server (default) | The server reads the working tree in place, so the code never leaves the machine |
| Cloud/remote server | The server clones the checkout's git remote (`https` URL): pushed commits on the default branch only, and it must be cloneable without credentials |
| Cloud/remote server, checkout with no git remote | Not offered; nothing the server could clone |
| Already-indexed repo | Refreshed without asking — after a turn that changed the tree (local), or at the session start after a push (cloud) |
| Not a git repo / no code files | Not offered |

`COGNEE_CODE_AUTOINDEX=always` indexes new repos without asking (repos of
more than 3000 source files are then skipped); `off` never asks and never
indexes. Indexed repos keep refreshing either way.

## What the graph reflects (freshness)

The graph tracks different things depending on where the Cognee server runs. Both are
normal, expected behavior — but the results look identical, so know which one applies:

| Server | Graph reflects | Stays current via |
|--------|----------------|-------------------|
| **Local** (default) | The working tree, **uncommitted and untracked changes included** | Automatic re-index after any turn that changed a file |
| **Cloud / remote** | The **last pushed commit** on the default branch — the server clones the repo and cannot read this machine's disk | Pushing; the checkout is re-indexed at the next session start (or run the index command again) |

On a cloud server, local edits are invisible to the graph until they are pushed. Nothing
errors and nothing looks unusual: a question about a symbol you just renamed locally is
answered from the pushed state, confidently. So when the answer matters and the work is
in progress, either push first or say so in the answer. `{"operation": "delta"}` shows
what the last index actually changed and is the fastest way to check what the graph
currently knows.

A cloud server therefore never gets a local path: given one, the index command sends
the checkout's git remote, and fails when there is none.

## Query the code graph

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "<seed>" 10 --code [--code-query '<json>']
```

The repository's code dataset is resolved from the current directory, so
`--dataset` is only needed to query a repo you are not standing in.

Without `--code-query`, the query text is a seed name (exact/suffix/substring
match). With it, pick one exact operation:

| Operation | Answers | Example |
|-----------|---------|---------|
| `query_facts` | filtered listing | `{"operation":"query_facts","kind":"route","limit":50}` — all API endpoints |
| `explore` | neighborhood of one node | `{"operation":"explore","name":"UserService","max_depth":1}` |
| `traverse` | follow edges from seeds | `{"operation":"traverse","start":"main","direction":"forward","max_depth":3}` |
| `find_path` | how A reaches B | `{"operation":"find_path","source":"AuthMiddleware","target":"Database"}` |
| `impact_analysis` | what breaks if X changes | `{"operation":"impact_analysis","targets":["process_payment"]}` |
| `architecture` (>=1.6.3) | module, route, storage and service map | `{"operation":"architecture","diagram":"mermaid"}` |
| `insights` (>=1.6.3) | explainer findings with cited facts | `{"operation":"insights","diagram":"mermaid"}` |
| `delta` | what the last index changed | `{"operation":"delta"}` |

An ambiguous seed returns an error **listing the candidates** — pick one and
retry with its exact id or a `repo` filter. A seed that doesn't resolve
returns empty (not an error): the graph simply has no such symbol.

## When to use which search

- **Structural question naming a symbol/file** ("what calls X", "what breaks
  if I change X", "list all endpoints") → `--code`. Exact, instant, no tokens.
- **Conceptual question naming nothing** ("how does auth work here?") →
  regular `cognee-search.sh` (hybrid/graph). Remember: graph-only code is
  invisible there unless indexed with `--index-vectors` or docs were ingested.
- **Chain them**: hybrid discovers the name ("the payment flow" →
  `PaymentProcessor`), then `--code` gives the exact structure around it.
- **Verify freshness**: after edits, `{"operation":"delta"}` shows what the
  last re-index added/updated/removed. Use it when facts look stale.

Treat results as a map, not ground truth — verify important claims against
the actual files before editing.

## Per-file ingestion (no repo index)

To store a single code file in normal memory under its real filename (routes
down the zero-LLM code path server-side, no cross-file edges):

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-remember.sh" --file src/payments.py --node-set project_docs
```

Prefer the repo index above when you care about callers/imports across files.

## Automatic recall

Once a repo is indexed from its checkout, the per-prompt memory recall adds a
code lane automatically when your prompt mentions an identifier-shaped token
(`process_payment`, `UserService`, `billing/api.py`) — the facts appear in the
injected context as `=== Code graph facts ===`. No action needed.
