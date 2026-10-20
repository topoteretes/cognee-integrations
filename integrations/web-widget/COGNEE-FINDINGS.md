# Building the web widget against cognee: what got in the way

Findings from building the widget's operator dashboard, docs ingestion, code-graph
indexing and conversation storage against a Cognee Cloud tenant
(`tenant-f44d1b1d-…aws.cognee.ai`), September 2026.

Everything below was observed on that tenant, not inferred from the source. Where
the deployed behaviour differs from `cognee` on `feature/sdk-802-company-brain-guide`,
both are given. Ordered by how much damage each one did.

---

## 1. `persist_session_qa` advances its watermark past turns it never wrote

**The worst one: it loses data, silently and unrecoverably.**

A three-turn conversation (`web:demo:visitor-babepv87:conv-7d7fgmfw`) produced one
persisted turn document. An explicit `POST /v1/improve` added a second. A third
identical call added nothing at all — the watermark now considered the session
covered, so the third turn can never be written.

```
session cache:        3 turns      (all present, confirmed via /v1/sessions/{id})
turn documents:       2            (1 automatic, 1 after a forced improve)
further improve runs: no change
```

Distillation saw all three — the lesson it produced covers the content of turns 2
and 3 — so the turns were readable when the persist stage ran. It wrote two and
marked three as done.

**Impact.** Any consumer relying on the session bridge for durable transcripts
will lose turns without an error, and re-running the bridge cannot repair it.

**Suggested fix.** Advance the watermark from what was actually written, not from
what was read. A partial write should leave the remainder uncovered.

---

## 2. Deleting a dataset silently destroys session memory

Clearing `web:demo:docs` invalidated every session attributed to it. The session
*rows* survived, complete with token totals; their Q&A turns did not.

```
web:demo:visitor-cgbz7qym:conv-ahelwt8l   84,957 tokens   0 turns
web:demo:visitor-72f9ze90:conv-9numjjva   26,566 tokens   0 turns
web:demo:visitor-2c6cd6ih:conv-0rjntsn7   16,642 tokens   0 turns
web:demo:visitor-px5ujn7h:conv-kh5hm9x8   23,435 tokens   0 turns
```

About 150k tokens of real conversation, gone. The rationale in
`invalidate_sessions.py` is sound — session memory quoting deleted documents makes
completions assert dead facts — but three things make it harsh in practice:

- **No warning.** The delete API says nothing about session memory.
- **No export.** There is no "persist before invalidating" step.
- **The residue is misleading.** A session row with tokens and no turns is
  indistinguishable from one that never had a turn, so a dashboard listing them
  cannot tell a wiped conversation from an empty probe.

**Suggested fix.** Persist attributed sessions before invalidating them, or at
minimum return what will be invalidated so a caller can save it first. Marking the
rows as invalidated would also let consumers explain the gap.

---

## 3. `node_set` is accepted and dropped on `content_type='code'`

`POST /v1/remember` with `content_type='code'` accepts a `node_set` and ignores it.
After indexing `github.com/topoteretes/cognee`, the graph held 43 `NodeSet` nodes —
all of them docs folders, none the `code/topoteretes/cognee` we passed.

**Impact.** A code graph cannot be scoped for recall by the tag you gave it, and
nothing tells you so. We removed the parameter rather than ship a promise the API
doesn't keep.

**Suggested fix.** Honour it, or reject the request. Silently dropping a parameter
is the worst of the three options.

---

## 4. `/v1/datasets/status/progress` doesn't exist on Cloud

Documented in the source with `completed_items`, `total_items` and `current_stage`.
On the tenant:

```
GET /api/v1/datasets/status/progress   → 404 Not Found
GET /api/v1/datasets/status            → 200 {"<id>": "DATASET_PROCESSING_COMPLETED"}
```

We built an ingest progress watcher against the documented endpoint and it reported
nothing, forever, because our client mapped 4xx to an empty result. Our bug to
swallow it — but the mismatch is what created it.

**Suggested fix.** A capability or version endpoint, so a client can tell which API
it is talking to instead of discovering it from a 404. Failing that, keep the OSS
docs aligned with what Cloud serves.

---

## 5. Two vocabularies for a pipeline state

`/v1/datasets/status` answers `DATASET_PROCESSING_COMPLETED`; the progress endpoint
(where it exists) answers `completed`. A client speaking to both has to normalise.

**Suggested fix.** One vocabulary, or one endpoint.

---

## 6. `/v1/improve` returns a pipeline run, not the documented result

The source documents an `ImproveResult` with one `stages[]` entry per stage,
each carrying `status`, a `reason` when skipped, and `counts`. The tenant returns:

```json
{"<dataset id>": {"status": "PipelineRunCompleted", "pipeline_run_id": "…", …}}
```

**Impact.** This is precisely what we needed to diagnose finding #1. With
`stages[]` we could have read *why* `persist_session_qa` declined; without it we
had to infer from what appeared in the dataset.

---

## 7. Learning what is in a graph costs the whole graph

There is no cheap way to ask "which repositories are indexed" or "what node types
exist". On this tenant:

```
GET /datasets/{id}/schema        → 404
POST /search  (searchType=CYPHER) → 400 "not supported on this deployment"
GET /datasets/{id}/graph          → 200, 46 MB, 23.6 s
```

The graph endpoint takes no filters, so reading one label costs the same as
counting 35,272 nodes. We cache the summary for 15 minutes and warm it at startup;
the first read still costs 24 seconds.

**Suggested fix.** Any one of: node-type filters on the graph endpoint, a schema
or summary route, or a counts endpoint. Even `?type=CodeRepository` would have
removed the whole cache layer.

---

## 8. `recall_history` carries no session id

`GET /v1/recall` returns `{id, text, user, createdAt, datasetId}` — and takes no
parameters at all: no session filter, no date range, no pagination.

It is the one durable record of what visitors asked (ours reaches back three
months, well past anything in the session cache), but with no session id the turns
cannot be grouped into conversations. We tried correlating by time against session
windows; on real data **6 questions were claimed by two sessions at once and 20 of
47 fell into no session window**, because one long-running session's window
swallowed three short ones.

**Suggested fix.** Add `session_id` to the row. It would make the durable record
usable, and it is the single highest-value change on this list for us.

---

## 9. Session TTL behaviour is unclear

`session_ttl_seconds` defaults to 604800 (7 days), refreshed on write. But sessions
last active on 2026-09-19 were still listed on 2026-09-27 — eight days idle.

So either the TTL is disabled on this tenant, or it governs cached entries rather
than the session row. Either is fine; not knowing which makes it impossible to say
how long a caller has before a conversation is unrecoverable.

**Suggested fix.** Document which objects the TTL governs, and expose the effective
value.

---

## 10. No conditional write, so concurrent appends lose data

We keep one document per conversation and append each turn via
`PATCH /v1/update`. Two turns answered close together both read the document before
either wrote, and the second overwrote the first:

```
conversation written without a lock:  2 of 3 turns
conversation written with a lock:     3 of 3 turns
```

An in-process lock fixes it for one backend. Two backends writing the same document
would race again, and there is no `If-Match`, version or ETag to make the write
conditional.

**Suggested fix.** A version or content hash on the document that `PATCH` can be
conditioned on.

---

## 11. The chunker cannot be chosen

`chunk_size` is exposed on `/v1/remember` and `/v1/cognify`. The chunker itself is
not: it follows the document type, which follows the **sniffed** extension —
`extension=file_type.extension` in `get_file_metadata`, read from the bytes, never
from the filename.

So a `.csv` or `.json` uploaded as text is a `TextDocument` cut by `TextChunker`,
even though `CsvChunker` and `JsonListChunker` exist. There is no parameter to say
otherwise.

**Suggested fix.** A `chunker` parameter, or classification that respects a
declared content type.

---

## 12. Deleting a dataset holds the request open until it is gone

`POST /v1/forget` with a dataset runs the whole delete inside the request. On `dev`
the handler awaits `forget()` and only then responds; unlike `remember` and
`improve` there is no `run_in_background`, and nothing to poll.

Clearing `web:demo:docs` — about 35k nodes, 25k of them the `topoteretes/cognee`
code graph — took longer than our client's 120-second timeout. The request failed
on our side while cognee carried on and finished: the dataset was gone from the
listing and from platform.cognee.ai, but the dashboard reported an error.

**Our side, needs fixing.** `dashboard_clear` drops its cached graph summary only
after `forget_dataset` returns, so the timeout skipped it and the repositories panel
kept showing the deleted code graph for up to 15 minutes. Its docstring is also
wrong: it says cognee "accepts the delete and drains the dataset behind it", which
is not what the endpoint does. The fix is to drop the cache whatever the outcome,
give the delete a timeout of several minutes, and report a timeout as "cognee may
still be deleting" rather than as a failure.

**Impact.** A client cannot tell a failed delete from a slow one. It has to either
wait an unknown time or guess, and whatever it caches about the dataset goes stale
either way.

**Suggested fix.** Accept the delete and return at once, with a pipeline run or
status the client can poll — as `remember` and `improve` already offer.

---

## 13. Smaller friction

- **`remember(session_id=…)` writes no document of its own.** It is purely a
  trigger for the bridge; the text passed to it does not become an item. Two
  exchanges produced two items and both were the bridge's. Worth documenting —
  we built on the opposite assumption.
- **`IMPROVE_DEBOUNCE_ENTRIES` defaults to 1 and `_SECONDS` to 0**, so the
  automatic bridge runs a full improve on *every* turn. They are server-side env
  vars, so a shared tenant cannot tune them per integration.
- **Nothing runs on a schedule.** There is no periodic worker and no pre-expiry
  hook; the bridge only ever runs because a caller triggered it. A conversation
  that nobody distils before the cache drops is simply lost.
- **A code graph stores structure, not content.** `CodeSymbol`, `CodeFileReference`
  and friends carry `file_path`, `line` and `description` but no source text, and
  create no dataset items — so `fetch_raw` has no id to ask for. Reasonable by
  design; worth stating plainly in the docs, because "the repo is indexed"
  suggests otherwise.
- **`content_type='code'` accepts no uploads**, only a git URL or a server-local
  path. For a hosted tenant that means git URLs only, which rules out indexing a
  local checkout from a browser-based tool.

---

## 14. Things that worked well, and are worth keeping

- **The content hash is already exposed.** `rawDataLocation` ends in
  `text_<md5>.txt`, and the digest is the md5 of the stored bytes — verified on all
  255 items of a corpus, all distinct. This is what our change detection is built
  on, and it works exactly. It appears to be incidental rather than a contract,
  which is the only worry: **please make it one.**
- **`external_metadata` round-trips and comes back in the data listing.** It is how
  we keep a document's real source path after cognee strips the extension from the
  name, with no extra call.
- **Chunk-level diff on `/v1/update` does what it says.** Appending a turn
  re-ingests only the new chunk; earlier chunks keep their ids.
- **The persist stages are idempotent** where the watermark is right — a repeat run
  writes nothing and does not re-embed the dataset.
- **Distillation output is genuinely good.** One three-turn conversation produced a
  lesson that captured what the docs failed to answer, correctly and concisely.

---

## What we would ask for first

1. **`session_id` on `recall_history` rows** — unlocks the durable record.
2. **Fix the `persist_session_qa` watermark** — it loses data today.
3. **A filter on the graph endpoint** — removes an entire caching layer.
4. **A documented content hash on the data listing** — we depend on it already.
5. **A conditional write on `/v1/update`** — makes concurrent appends safe.
