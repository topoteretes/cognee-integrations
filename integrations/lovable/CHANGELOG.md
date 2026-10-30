# Changelog

All notable changes to **@cognee/lovable-memory** (`integrations/lovable`) are documented here.

The version must match `package.json`, `src/version.ts` (`PACKAGE_VERSION`) and the
`current_version` for `lovable` in [`integrations/inventory.yml`](../inventory.yml);
`tests/version.test.mjs` fails when they disagree. Tag releases as `lovable-v<version>`.

The format is based on [Keep a Changelog](https://keepachangelog.com/) and the project
follows [Semantic Versioning](https://semver.org/).

## [0.1.0]

### Added
- Initial release of the Lovable builder-memory connector: a single-tenant remote MCP
  service (Streamable HTTP, stateless, bearer-token authenticated) that exposes
  `recall_product_context`, `remember_product_decision` and `memory_status` over a
  fixed project dataset plus an explicit read-only shared dataset list.
- SQLite receipt ledger (`node:sqlite`) tracking every accepted write through
  `accepted → indexing → ready | failed | unknown | deleted`, with idempotent replay by
  `request_id` and content-hash deduplication.
- Operator CLI `lovable-memory` (`serve`, `health`, `token`, `receipts`, `status`,
  `reconcile`, `forget --yes`, `config`); deletion is verified by re-listing the dataset.
- Docker image and Compose example with a persistent receipt volume, health check and a
  Caddy TLS reverse-proxy snippet; `examples/project-knowledge.txt` for Lovable's
  project knowledge.
- Wire contract verified live against cognee 1.6.5; captured fixtures under
  `fixtures/cognee-1.6.5/` drive the unit and protocol tests.

### Fixed (pre-release review)
- The request deadline now covers the response body as well as the headers: a
  backend or proxy that stalls mid-body times out instead of hanging a tool call.
- Content deduplication hashes the normalized decision (kind, content, rationale,
  source reference), not the serialized document, so identical decisions saved at
  different times are one record; the capture time is still stored in the text.
- Document lookups report `found`, `absent` or `exhausted` (listing longer than the
  safety cap of 500 pages). `memory_status` keeps a receipt's status and records
  the reason when the listing is exhausted, and `forget` does not claim a verified
  deletion on an exhausted listing.
