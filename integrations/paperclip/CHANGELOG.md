# Changelog

All notable changes to **@cognee/paperclip-plugin** (`integrations/paperclip`) are documented here.

The version must match `package.json`, `src/version.ts` (`PACKAGE_VERSION`, which the manifest
reads) and the `current_version` for `paperclip` in
[`integrations/inventory.yml`](../inventory.yml); `tests/version.test.mjs` fails when they
disagree. Tag releases as `paperclip-v<version>`.

The format is based on [Keep a Changelog](https://keepachangelog.com/) and the project
follows [Semantic Versioning](https://semver.org/).

## [0.1.0]

### Fixed
Pre-release review findings, fixed before the first tag (version unchanged; the
`001_memory_ledger.sql` migration was extended in place rather than adding a second
migration, since no installation has run it yet):

- A replacement revision was sent while its predecessor's retirement was still held:
  `otherRevisions` excluded `superseded` rows, so the blocker check never matched. The
  query now includes superseded and conflict rows and the replacement stays `pending`.
- The deleted-source pass of `reconcile-sources` re-checked the same oldest page of
  `ready` rows every run. Rows now carry `last_source_check_at`; the pass orders by it
  (never-checked first) and excludes decisions in the query, so every row rotates through.
- The request deadline was dropped once response headers arrived; a stalled body could
  hold a tool call or job open indefinitely. One deadline now covers headers and body, and
  the body reader is cancelled when it fires.
- The uncertain-write recovery grace was measured from `updated_at`, which every check
  rewrote, so frequent reconciliation could postpone recovery forever. Rows now carry
  `sent_at` and the grace counts from the send.
- Document lookups stopped after 2,000 documents and reported "not found". Lookups now
  walk to a short page (cap 50,000 documents) and report `exhausted` distinctly; readiness,
  retirement and resend never treat `exhausted` as absence.
- `cleanup-scope` enumerated at most 5,000 documents and 5,000 ledger rows and could mark
  rows `retired` on a truncated verification listing. It now pages both to the end and, when
  the backend listing is incomplete, reports `incomplete: true` and retires nothing.

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
