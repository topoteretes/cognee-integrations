---
name: cognee-code
description: Index a code repository into Cognee's code graph and query it deterministically (callers, impact analysis, paths, endpoints). Use for structural code questions, "index this repo", and checking what a re-index changed.
---

# Cognee Code Graph

Build and query an architectural graph of a codebase (symbols, calls, imports,
endpoints, dependencies) via Cognee's enola-backed pipeline. Requires a Cognee
server >= 1.5.3. Indexing makes **no LLM or embedding calls** — it is fast,
deterministic, and token-free.

## Index a repository

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-index-repo.sh <repo-path-or-git-url> [--dataset <name>] [--index-vectors] [--wait <seconds>]
```

- **Local path** (e.g. `.` or `/path/to/repo`): works when the Cognee server
  shares this filesystem — the default local plugin server does. Cloud servers
  reject local paths; pass a git URL instead.
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

**Indexing is also the freshness opt-in**: for local-path repos the plugin
records a git fingerprint and automatically re-submits the repo in the
background when a turn changed the working tree, so the graph tracks your
edits. Re-indexing an unchanged repo is skipped server-side (content hashes),
so re-running the command is always safe.

## Automatic indexing

Opening the agent inside a git repository indexes it automatically in the
background at session start — no setup step. What happens depends on the
server, because indexing a repo means the code has to be readable by it:

| Situation | Behavior |
|-----------|----------|
| Local server (default), new repo | Indexed automatically — the server reads the working tree in place, so the code never leaves the machine |
| Cloud/remote server, new repo | **Not** indexed automatically; run the index command with a git URL, or set `COGNEE_CODE_AUTOINDEX=always` |
| Already-indexed repo | Always refreshed if the tree changed since the last index — regardless of the setting above |
| Not a git repo / no code files / very large repo (>3000 source files) | Skipped; index it explicitly if you want it |

Set `COGNEE_CODE_AUTOINDEX=off` to disable automatic indexing of new repos
(explicitly indexed repos keep refreshing). Explicit indexing has no size cap.

## What the graph reflects (freshness)

The graph tracks different things depending on where the Cognee server runs. Both are
normal, expected behavior — but the results look identical, so know which one applies:

| Server | Graph reflects | Stays current via |
|--------|----------------|-------------------|
| **Local** (default) | The working tree, **uncommitted and untracked changes included** | Automatic re-index after any turn that changed a file |
| **Cloud / remote** | The **last pushed commit** — the server clones the repo and cannot read this machine's disk | Pushing, then re-indexing |

On a cloud server, local edits are invisible to the graph until they are pushed. Nothing
errors and nothing looks unusual: a question about a symbol you just renamed locally is
answered from the pushed state, confidently. So when the answer matters and the work is
in progress, either push first or say so in the answer. `{"operation": "delta"}` shows
what the last index actually changed and is the fastest way to check what the graph
currently knows.

Local-path indexing therefore requires a server that shares this filesystem; cloud
servers reject local paths and need a git URL.

## Query the code graph

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh "<seed>" 10 --code [--code-query '<json>']
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
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-remember.sh --file src/payments.py --node-set project_docs
```

Prefer the repo index above when you care about callers/imports across files.

## Automatic recall

Once a repo is indexed from its checkout, the per-prompt memory recall adds a
code lane automatically when your prompt mentions an identifier-shaped token
(`process_payment`, `UserService`, `billing/api.py`) — the facts appear in the
injected context as `=== Code graph facts ===`. No action needed.
