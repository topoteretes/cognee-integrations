---
name: codebase
description: Use when ingesting, cognifying, or querying a codebase with Cognee CLI from Codex.
---

# Cognee CLI Codebase Workflows

Use this skill when the user asks Codex to build a Cognee memory of a repository,
index code, query implementation details, or create a code-aware knowledge graph.

## Rules

- Use `uv run cognee-cli ...`; do not use MCP.
- Start by defining the codebase scope and dataset name.
- Never ingest `.env`, private keys, credentials, local database files, virtualenvs, dependency caches, or generated build output.
- Prefer focused ingestion over sending an entire large repository at once.
- Use `rg --files` first to inspect candidate paths.

## Scope

Create a dataset name that is stable and readable, such as:

```text
codebase-cognee
codebase-frontend
codebase-api
```

Inspect candidate files:

```bash
rg --files
```

Common exclusions include:

```text
.git/
.venv/
node_modules/
dist/
build/
.next/
coverage/
.env
*.sqlite
*.db
*.key
*.pem
```

## Ingest

For a focused set of source paths:

```bash
uv run cognee-cli add <path-1> <path-2> <path-3> -d <dataset-name>
uv run cognee-cli cognify -d <dataset-name> --background
```

For small docs or architectural notes:

```bash
uv run cognee-cli remember <path-or-note> -d <dataset-name>
```

If command length becomes unwieldy, ingest in batches by directory or feature
area, then run `cognify` once for the dataset.

## Query

For code-specific questions:

```bash
uv run cognee-cli search "<implementation question>" -d <dataset-name> -t CODE -k 10 -f pretty
```

For architecture and reasoning questions:

```bash
uv run cognee-cli recall "<architecture question>" -d <dataset-name> -t GRAPH_COMPLETION -f pretty
```

For citation-like output:

```bash
uv run cognee-cli search "<specific symbol or behavior>" -d <dataset-name> -t CHUNKS -k 10 -f json
```

Use results as supporting context. Verify important claims against the actual
files before editing code.
