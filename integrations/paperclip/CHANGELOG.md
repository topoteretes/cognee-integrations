# Changelog

All notable changes to **@cognee/paperclip-plugin** (`integrations/paperclip`) are documented here.

The version must match `package.json`, `src/version.ts` (`PACKAGE_VERSION`, which the manifest
reads) and the `current_version` for `paperclip` in
[`integrations/inventory.yml`](../inventory.yml); `tests/version.test.mjs` fails when they
disagree. Tag releases as `paperclip-v<version>`.

The format is based on [Keep a Changelog](https://keepachangelog.com/) and the project
follows [Semantic Versioning](https://semver.org/).

## [0.1.0]

### Added
- Initial release of the Paperclip company-memory plugin (`cognee.company-memory`):
  captures completed issues and `#remember`-marked comments from enabled projects into
  per-project cognee datasets, with provenance and links in the stored text.
- Agent tools `cognee_recall` (scoped to the run's project dataset plus an optional
  company-shared dataset) and `cognee_remember_decision` (tied to an authorized issue).
- Durable ingestion ledger in the plugin's database namespace
  (`migrations/001_memory_ledger.sql`) with a unique source/revision key; states
  `pending → accepted → indexing → ready`, plus `uncertain`, `failed`, `conflict`,
  `superseded`, `retired`, `revoked`. Replaced sources are retired by verified targeted
  deletion before the replacement is sent.
- Scheduled jobs `drain-outbox` (every 5 minutes) and `reconcile-sources` (hourly), an
  `onHealth` report, `onValidateConfig` with a live backend check, and operator actions
  `sync-project` (backfill) and `cleanup-scope` (revoke queued writes, delete and verify).
- Agent skill `skills/company-memory/SKILL.md` and an importable example company under
  `examples/research-company/`.
- Wire contract verified live against cognee 1.6.5; fixtures under
  `fixtures/cognee-1.6.5/`; SDK test-harness suite plus an opt-in live test.
