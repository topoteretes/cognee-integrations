---
name: cognee-forget
description: Forget data stored in Cognee memory. Use when the user asks to forget, delete, or remove something from memory (e.g. "forget what we talked about tennis", "delete that from memory"). Finds the documents in the current dataset whose raw content matches what the user wants forgotten and deletes them.
---

# Cognee Forget

Delete documents from Cognee memory based on what the user asks to forget. You decide *which* documents match by reading their raw content — Cognee only deletes what you tell it to.

## When to use

- User says "forget ...", "delete ... from memory", "remove what you know about ...", "stop remembering ..."
- NOT for clearing an entire dataset or all memory unless the user explicitly asks for that (see the warning at the bottom)

## How it works

Forgetting is a three-step process against the **running Cognee server**: find the current dataset's id, inspect the raw data of each document in it, then forget the documents whose content matches what the user wants deleted. Every endpoint used below is documented in the server's Swagger UI at `${COGNEE_BASE_URL:-http://localhost:8011}/docs` (local server: `http://localhost:8011/docs`; cloud: `$COGNEE_BASE_URL/docs`) — consult it if a request shape doesn't match your server version.

## Instructions

### 1. Find the dataset id of the currently used dataset

The plugin's dataset is `$COGNEE_PLUGIN_DATASET` if set, otherwise `agent_sessions`. Resolve its UUID:

```bash
curl -s "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/datasets" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}"
```

Pick the dataset whose `name` matches `${COGNEE_PLUGIN_DATASET:-agent_sessions}` and note its `id`.

### 2. List the data in the dataset and read the raw content

```bash
curl -s "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/datasets/<dataset_id>/data" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}"
```

Each item has an `id` (the data id) and metadata. For each item, download its raw content (the original stored text, e.g. the session transcript) and check whether it mentions what the user wants forgotten:

```bash
curl -s "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/datasets/<dataset_id>/data/<data_id>/raw" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}"
```

**Judge by meaning, not just keywords.** "Forget what we talked about tennis" should match a document discussing rackets and Wimbledon even if the word "tennis" never appears. When a document only mentions the topic in passing amid unrelated content, prefer keeping it and tell the user why.

### 3. Confirm, then forget each matching document

Deletion is irreversible. Before deleting, show the user the list of matching documents (data id + a one-line summary of each) and get their confirmation — unless the user's request was already specific and unambiguous.

Then forget each matching document by its data id:

```bash
curl -s -X POST "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/forget" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}" \
  -d '{"datasetId": "<dataset_id>", "dataId": "<data_id>"}'
```

Repeat for every matching data id. A 2xx response means that document and its derived knowledge are removed.

### 4. Report the outcome

Tell the user exactly what was deleted (and what was checked but kept, if the match was fuzzy). If nothing matched, say so — do not delete "closest" documents on a miss.

## Error handling

- **401 Unauthorized**: the server requires `COGNEE_API_KEY` (auth is enforced even on localhost). Never retry without the key.
- **404 on a data id**: it was already deleted (possibly by an earlier step of this same run) — not an error, move on.
- An `{"error": ...}` response means the server was reachable but the request failed — surface it; do **not** conclude the data was deleted.

## Broader deletions — explicit user request only

The `/api/v1/forget` endpoint can also delete more than a single document (see `${COGNEE_BASE_URL:-http://localhost:8011}/docs`):

| Payload | Effect |
|---------|--------|
| `{"datasetId": "...", "dataId": "..."}` | Forget one document (the default for this skill) |
| `{"datasetId": "..."}` | Delete the **entire dataset** |
| `{"datasetId": "...", "memoryOnly": true}` | Clear the dataset's memory (graph + vector) but keep raw data |
| `{"everything": true}` | Delete **all** user data |

Never use the dataset-wide or `everything` forms unless the user explicitly and unambiguously asked for that scope, and confirm first either way.
