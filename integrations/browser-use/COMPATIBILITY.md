# Compatibility

What this release was built against and what was actually exercised. A row is a tested
combination, not a promise about neighbours.

## Adapter runtime

| Component | Version | Notes |
|---|---|---|
| Python | **3.11+** (CI and local verification on 3.12) | Follows `browser-use`'s own floor. |
| `browser-use` | **>=0.13.11,<0.14** (tested 0.13.11, the latest published wheel on 2026-10-24) | Public surfaces used: `Tools.action` (custom actions, auto-generated param models), `ActionResult` (`extracted_content`, `error`, `include_extracted_content_only_once`; never `is_done`), `Agent(extend_system_message=…, tools=…)`, `Agent.run(max_steps, on_step_start, on_step_end)`, `AgentHistoryList.final_result/is_done/is_successful/number_of_steps/urls/action_names/extracted_content`, `agent.state.n_steps`, `agent.browser_session.get_current_page_url()`. No private message-manager patches. |
| `httpx` | >=0.27,<1 (0.28.1 is what browser-use pins) | Async transport; `MockTransport` in contract tests. |
| SQLite | stdlib `sqlite3` | Receipt/outbox store; WAL on file-backed stores. |

Not depended on: `cognee`, `cognee-mcp` (the adapter only speaks HTTP to a running server).

## Cognee backend

| Backend | Version | Verified |
|---|---|---|
| Self-hosted cognee | **1.6.5** | Live on 2026-10-24: dataset create, background `remember`, cognify status, data listing by upload name, `CHUNKS` recall with explicit `datasets` + `scope: ["graph"]`, targeted `forget` verified by re-listing, cross-process recall, scope isolation. Fixtures in [`tests/contract/fixtures/`](tests/contract/fixtures/README.md). |
| Cognee Cloud | not verified in this release | Same `/api/v1` routes and `X-Api-Key` auth; run `tests/live` against your tenant before relying on it. |

Observed 1.6.5 behaviours the adapter is built around:

- A background `remember` answers `{"status":"running","dataset_id":…,"pipeline_run_id":null}` with
  **no document id**; the adapter finds its document later by the content-derived upload
  name (`browser-<kind>-<sha256[:32]>`) in the dataset listing. The document bytes carry
  no record id, run id or timestamp, so identical content is byte-identical under one name
  and the server's content-derived data id yields one document; a reused upload name with
  *different* content is dropped silently on the background path, which content-derived
  names avoid.
- `POST /forget` answers `status: "success"` even for an unknown `dataId`; deletion is
  verified by re-listing. A deleted document stops appearing in `CHUNKS` recall at once.
- `POST /recall` naming a dataset that does not exist answers HTTP 404 with
  `DatasetNotFoundError` in the body; the adapter sends the scoped request straight away (no
  dataset lookup first), treats that 404 as "nothing stored yet" and never retries unscoped.
  A completion search on an emptied dataset answers 404 `NoDataError`, also reported as
  empty. Any other 404 (wrong base URL prefix, proxy) is reported as a recall error.

## Browser Use surfaces

| Surface | Status |
|---|---|
| Open-source Python `Agent` with a local browser | Adapter target. Live two-run demo with a real `Agent`, `ChatOpenAI` and a local fixture site: see the release notes in the implementation report; absence of that line means it was not run. |
| Local agent with a remote (CDP) browser | Not tested; the adapter does not touch the browser session beyond reading the current URL. |
| Browser Use Cloud hosted tasks, the browser harness CLI, JavaScript surfaces | Not supported by this release. |

## Judgement calls

- Graph-only recall (`CHUNKS`, explicit datasets): each process retrieves independently of
  prior agent state; no session cache is used. `GRAPH_COMPLETION` is not used for injected
  context because its text is a synthesized answer rather than evidence with provenance.
- `cognee_remember` is opt-in (`remember_tool_enabled=False` by default), may create at
  most `max_remember_calls_per_run` (3) new documents per run, and stores
  model-proposed facts as `unverified`; final-outcome capture defaults to on and records
  `agent_reported` only when the agent reports success, `application_validated` only when
  the caller's validator says so.
- Per-site isolation is not a hard filter: domain names are retrieval hints. Use separate
  scopes for strict isolation.
- No automatic expiration: records carry an observation time and the instructions tell the
  agent to re-check changing facts.
- Receipt status is derived from the dataset-level `cognify_pipeline` status (the only
  one cognee 1.6.5 exposes) plus the document listing. `ready` therefore means "listed and
  the dataset's latest run completed"; a dataset-level error never marks a document
  `failed`, it keeps the receipt `indexing` with the reason until the grace period moves it
  to `unknown`. An `unknown` receipt (timed out, cancelled or never listed) does not count
  as stored: identical content is uploaded again, and the server's one-document-per-content
  behaviour keeps that idempotent. It is still reconciled; if the document has not appeared
  24 hours after the write it becomes `lost`, which is terminal. Because several receipts
  can share one document, a verified `forget_record()` retires every receipt of that
  content in the dataset.
