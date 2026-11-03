# Changelog

All notable changes to **cognee-integration-browser-use** (`integrations/browser-use`) are
documented here.

The version must match `pyproject.toml`, `cognee_integration_browser_use/__init__.py`
(`__version__`) and the `current_version` for `browser-use` in
[`integrations/inventory.yml`](../inventory.yml). Tag releases as `browser-use-v<version>`.

The format is based on [Keep a Changelog](https://keepachangelog.com/) and the project
follows [Semantic Versioning](https://semver.org/).

## [0.1.0]

### Added
- Initial release of the Browser Use memory adapter: `CogneeBrowserMemory` composes
  around `browser_use.Agent` (no subclass) with one bounded graph recall before the run,
  `cognee_recall` / opt-in `cognee_remember` actions registered into the caller's
  `Tools`, cheap step observation, and a compact provenance-stamped outcome written to
  a Cognee server afterwards. `run.execute()` composes the caller's step hooks, captures
  in `finally`, returns the agent's own history and preserves the original exception or
  cancellation; memory outcomes live on `run.status`.
- Trusted scope (`MemoryScope(tenant_id, user_id, project_id)`) mapped to an opaque,
  stable dataset name; the model never chooses a dataset.
- Async httpx transport verified against cognee 1.6.5 (fixtures under
  `tests/contract/fixtures/`), SQLite receipt/outbox store with an explicitly
  non-durable in-memory mode, URL and text sanitization, host-side
  `forget_record()` with ownership check and deletion verified by re-listing.
- Examples (`repeated_supplier_research.py`, `custom_run_loop.py`), a three-condition
  evaluation harness skeleton under `evals/`, unit, contract and opt-in live tests.

### Fixed (pre-release review)
- Every text field of a memory record is redacted at the serialization boundary:
  the task, the outcome, the summary and exception-derived fallback text, not only
  the summary. A task such as `login password=... token=...` no longer persists
  those values.
- Recall queries (the task, the model's `cognee_recall` query and the query hints) are
  redacted before they are sent: the backend records search queries, so a secret in a
  query was stored through the read path. The redaction regression test now checks the
  recall payload as well as the stored document.
- The redaction rule set is the Hermes integration's: prefixed keys such as
  `access_token=` and `client_secret:` (the old `\b` rule missed them), `passwd`/`pwd`,
  `Authorization: Bearer …`, connection strings, vendor key formats and a private key
  clipped before its `END` marker.
- `content_hash` covered the record id, run id and timestamp, so the ledger dedupe could
  never match and every repeat paid a new cognify pass. The hash now covers scope, kind,
  verification, task, summary, outcome and source URLs; identical observations from a
  repeated `cognee_remember` call or a later run reuse the existing record.
- `cognee_remember` may create at most `max_remember_calls_per_run` (default 3) new
  documents per run and answers with a tool error past that; deduplicated repeats are
  free and the final-outcome record is not counted.
- `reconcile()` fetched the dataset status and re-paged the listing once per pending
  receipt. It now groups receipts by dataset, calls the status endpoint once and walks the
  listing once (stopping when every pending name is found).
- Document lookups stopped after 2,000 rows and reported "not found". Lookups now walk to
  a short page (cap 50,000 documents) and report `found`, `absent` or `exhausted`;
  `forget_record()` answers `verified: False` on an exhausted listing instead of marking
  the receipt deleted, and status checks keep the receipt pending with the reason.
- A dataset-level pipeline error no longer stamps a receipt `failed`, which was terminal
  and never rechecked although the dataset is shared by every run in the scope. The
  receipt stays `indexing` with the reason and moves to `unknown` after the grace period;
  `failed` is reserved for writes the backend rejected.
- The request deadline is enforced over the whole exchange (`httpx.Timeout` bounds each
  read separately), so a response that trickles in can no longer hold a tool call or
  `execute()` open past the configured timeout.
