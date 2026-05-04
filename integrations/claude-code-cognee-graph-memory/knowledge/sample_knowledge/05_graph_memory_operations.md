# Graph memory operations know-how

## When to call cognify

`cognify` runs entity extraction and graph construction over the input text.
Run it after a batch of `remember` calls, or directly on a long document, to
materialize new entities and relationships into the graph. Without `cognify`,
`search(GRAPH_COMPLETION)` and `recall` cannot reason over freshly added data.

## remember vs save_interaction

`remember` stores arbitrary text in the permanent memory. Use it for documents,
rules, decisions, and incident records. `save_interaction` stores a Q-A pair
into the session cache, optimized for short conversational turns. Use
`save_interaction` when capturing a single exchange, and `remember` for any
content that must survive into the permanent graph immediately.

## search type selection

Use `search(CHUNKS)` to retrieve the raw matching passages without LLM cost.
Use `search(GRAPH_COMPLETION)` when the question requires reasoning across
multiple entities or summarizing relationships. CHUNKS is deterministic and
cheap; GRAPH_COMPLETION involves the configured LLM and may fail when the
local model returns malformed structured output.

## recall auto-routing

`recall` chooses a search strategy automatically. It can fall back to keyword
matching against session caches before hitting the permanent graph. Prefer
`search(CHUNKS)` for predictable retrieval and reserve `recall` for cases
where a session-aware shortcut is acceptable.

## forget_memory and graph_only

`forget_memory` removes data from the relational, graph, and vector stores.
The `graph_only=True` flag erases graph edges while preserving the underlying
chunks and embeddings, which is useful when graph quality degrades but the
raw text should remain searchable. Use the default (full delete) when the
content itself is no longer needed.

## improve usage

`improve` enriches the graph with triplet embeddings. Call it without
`session_ids` to run only the enrichment stage. Pass comma-separated session
IDs to additionally bridge session-cached Q-A pairs into the permanent graph.
Run `improve` periodically when many new sessions accumulate to keep the
graph aligned with recent activity.

## prune as reset

`prune` deletes both the data layer and the system metadata. It is the only
way to fully reset the local Cognee state during development. After `prune`,
the next `search` or `recall` will fail until at least one `remember` and
`cognify` cycle has run again.
