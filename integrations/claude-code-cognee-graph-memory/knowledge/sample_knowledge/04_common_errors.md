# Common errors and how to handle them

## Cognee: SearchPreconditionError

Calling `recall` / `search` right after `prune` (or before any data has been
ingested) raises SearchPreconditionError. You must register data with
`remember` first, then call `recall` / `search`.

## Cognee: DatabaseNotCreatedError

Calling `list_data` after `prune` raises DatabaseNotCreatedError. Because
`prune` deletes the database completely, you must run at least one `remember`
before `list_data` to re-initialize the database.

## Ollama connection error

If `import_to_graph.py` reports "Cannot reach Ollama," the Ollama service is
not running. Run `ollama serve` and retry. Also check whether `qwen2.5:14b`
(or the configured local LLM) has been downloaded with `ollama list`.

## recall returns empty results

If `recall` returns an empty result (`search_result: ['']`), the graph
construction may still be running. `remember` builds the graph asynchronously
in the background, so right after a large bulk ingest, check completion with
`cognify_status` before calling `recall`.

## LLM format error (recall failure)

`recall` can fail with an LLM JSON-format error when local LLMs smaller than
qwen2.5:14b do not respond in the JSON shape Cognee expects. As a workaround,
use `search(search_type="CHUNKS")` to retrieve text directly via vector search.

## LLM_ENDPOINT in .env requires /v1

When using Ollama as the LLM, `LLM_ENDPOINT` must be set to
`http://localhost:11434/v1` — not `http://localhost:11434`. Without `/v1`,
Cognee cannot find the OpenAI-compatible endpoint and errors out.
