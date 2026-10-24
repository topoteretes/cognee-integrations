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
