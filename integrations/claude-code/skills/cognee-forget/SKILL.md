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

Forgetting is a four-step process against the **running Cognee server**: sync the live session, find the current dataset's id, inspect the raw data of candidate documents, then forget the documents whose content matches what the user wants deleted.

Deleting a document removes its raw data, its derived graph knowledge, and — best-effort — the session memory *contaminated* by it: Q&A turns whose answers actually cited the deleted graph elements, plus the feedback and distilled guidance that descend from those turns. This is **targeted, not whole-session**: a turn that merely discussed the topic without citing the deleted elements survives, and agent **trace** entries are not matched at all. So deleting the documents is what makes forgetting durable — say that plainly when confirming, and don't promise the session cache is now free of the topic.

All server access goes through the `cognee-forget.sh` helper. It resolves the base URL and API key the same way the other skills do (shell env → `~/.cognee/.env` → the auto-minted local key in `api_key.json`) and **always** sends `X-Api-Key` — in cloud and local mode alike, since the server enforces auth even on localhost. Do not bypass it with raw `curl`: in local mode the key is never exported to your shell, so raw requests 401.

Every helper API command prints the JSON response followed by a final `HTTP <status>` line — always check it. The underlying endpoints are documented in the server's Swagger UI at `$COGNEE_BASE_URL/docs` (local: `http://localhost:8011/docs`) — consult it if a response shape doesn't match your server version.

## Instructions

### 1. Sync the live session first

Content from the current session that hasn't been persisted yet exists only in the session cache, where the listing below cannot see it. Flush it into documents first so it becomes findable and deletable:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-forget.sh sync
```

### 2. Find the dataset id of the currently used dataset

The plugin's dataset is `$COGNEE_PLUGIN_DATASET` if set, otherwise `agent_sessions`. Resolve its UUID:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-forget.sh datasets
```

Pick the dataset whose `name` matches `${COGNEE_PLUGIN_DATASET:-agent_sessions}` and note its `id`.

### 3. List the data in the dataset and read candidates' raw content

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-forget.sh data <dataset_id>
```

Each item has an `id` (the data id) and metadata. **Don't raw-fetch every document in a large dataset** — narrow first: a `cognee-search.sh "<topic>"` recall confirms the topic exists in memory and its hits hint at which sessions/documents hold it, and the listing's metadata (`name`, `createdAt`, and a session id field when present) lets you prioritize. Then download the raw content of the candidates (the original stored text, e.g. the session transcript) and check whether it mentions what the user wants forgotten:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-forget.sh raw <dataset_id> <data_id>
```

**Judge by meaning, not just keywords.** "Forget what we talked about tennis" should match a document discussing rackets and Wimbledon even if the word "tennis" never appears. When a document only mentions the topic in passing amid unrelated content, prefer keeping it and tell the user why.

**Group documents by session.** One session produces several documents (the Q&A transcript, trace feedbacks, distilled lessons). When a document matches, its siblings from the same session usually carry the same content in another form — check items sharing the session id (from metadata when present, or the `Session ID:` line at the top of the raw content) and include them in the same decision. Deleting one document of a session while keeping its siblings leaves the topic recallable.

### 4. Confirm, then forget each matching document

Deletion is irreversible. Before deleting, show the user the list of matching documents (data id + a one-line summary of each), note that session turns which relied on each document are cleared along with it, and get their confirmation — unless the user's request was already specific and unambiguous.

Then forget each matching document by its data id:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-forget.sh forget <dataset_id> <data_id>
```

Repeat for every matching data id. A final `HTTP 2xx` line means that document and its derived knowledge are removed.

### 5. Report the outcome

Tell the user exactly what was deleted (and what was checked but kept, if the match was fuzzy). If nothing matched, say so — do not delete "closest" documents on a miss. Note two limits honestly if they apply: content already injected into the current conversation's context stays visible until the conversation ends (forgetting governs what memory returns from now on, not what was already said), and tool-call **trace** entries still in the session cache are not invalidated by a document delete — if traces of the topic were captured this session, a later sync can re-persist them as new trace documents, which would need forgetting again.

## Error handling

- **Helper exits 2 ("no API key resolved")**: no key in `COGNEE_API_KEY`, `~/.cognee/.env`, or the local `api_key.json` cache. Cloud mode: add `COGNEE_API_KEY` to `~/.cognee/.env`. Local mode: the key is minted at session start — start a new session or run `cognee-doctor.sh`. Never fall back to unauthenticated requests.
- **HTTP 401**: a key was sent but rejected — it doesn't match this server. Check `COGNEE_API_KEY` against the target `COGNEE_BASE_URL`.
- **HTTP 404 on a data id**: it was already deleted (possibly by an earlier step of this same run) — not an error, move on.
- An `{"error": ...}` response body means the server was reachable but the request failed — surface it; do **not** conclude the data was deleted.

## Broader deletions — explicit user request only

The `/api/v1/forget` endpoint can also delete more than a single document (see `$COGNEE_BASE_URL/docs`). The helper deliberately supports only single-document deletion; for a broader scope, resolve credentials with the helper's `env` command and call the endpoint directly **in the same shell invocation** (exports do not persist across separate Bash calls):

```bash
eval "$(${CLAUDE_PLUGIN_ROOT}/scripts/cognee-forget.sh env)" && \
curl -sS -X POST "${COGNEE_BASE_URL}/api/v1/forget" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${COGNEE_API_KEY}" \
  -d '<payload>'
```

| Payload | Effect |
|---------|--------|
| `{"datasetId": "...", "dataId": "..."}` | Forget one document (the default for this skill — use the helper) |
| `{"datasetId": "..."}` | Delete the **entire dataset** |
| `{"datasetId": "...", "memoryOnly": true}` | Clear the dataset's memory (graph + vector) but keep raw data |
| `{"everything": true}` | Delete **all** user data |

Never use the dataset-wide or `everything` forms unless the user explicitly and unambiguously asked for that scope, and confirm first either way.
