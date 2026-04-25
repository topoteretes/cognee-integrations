---
name: memory
description: Use when Codex should remember, recall, search, improve, or forget information using the Cognee CLI.
---

# Cognee CLI Memory

Use this skill when the user asks Codex to use Cognee as memory, add facts or
documents, search a knowledge graph, recall prior context, or improve existing
memory.

## Rules

- Use `uv run cognee-cli ...`; do not use MCP.
- Prefer `remember` for one-step ingestion and graph construction.
- Use `add` plus `cognify` when ingestion and processing should be staged.
- Choose a clear dataset name with `-d` or `--dataset-name`; ask only if the dataset boundary is genuinely ambiguous.
- Do not ingest secrets, credentials, `.env` files, private keys, token dumps, or unrelated generated artifacts.
- Before destructive commands such as `forget`, `delete`, or `--everything`, get explicit user confirmation.

## Add And Build

One-step ingestion:

```bash
uv run cognee-cli remember <text-or-path> -d <dataset-name>
```

For larger or staged work:

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

Memory-oriented recall:

```bash
uv run cognee-cli recall "<question>" -d <dataset-name> -f pretty
```

Search modes:

```bash
uv run cognee-cli search "<question>" -d <dataset-name> -t GRAPH_COMPLETION -f pretty
uv run cognee-cli search "<exact passage or citation need>" -d <dataset-name> -t CHUNKS -k 10 -f pretty
uv run cognee-cli search "<code question>" -d <dataset-name> -t CODE -k 10 -f pretty
```

Use `-f json` when downstream parsing is needed.

## Improve Memory

Enrich an existing graph:

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
