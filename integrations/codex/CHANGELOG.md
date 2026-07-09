# Changelog

All notable changes to the **Cognee Plugin for Codex** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versions are tagged `codex-v<version>` (see [RELEASING.md](./RELEASING.md)).

## [Unreleased]

## [1.0.3-local]

### Added
- Capture Cognee session memory from Codex session events (start, prompt, tool
  result, stop).
- Standardized Cognee session names to `{agent}_{native_session_id}`.
- Status line surfacing Cognee state.
- `auto_feedback` env variable (default on).

### Changed
- Resilient recall client: circuit breaker + bounded timeout; HTTP/auth errors
  are not treated as authoritative and never silently fall back to the local CLI.
- Split `remember`/`sync` skills to mirror the `search` skill layout.

### Fixed
- Recall dataset targeting.
- SSL certificate handling for fresh environments.

## [1.0.0-local]

### Added
- Initial Codex Cognee plugin: CLI-first skills for setup, memory, codebase
  ingestion, and local UI launch (2026-04-24).

[Unreleased]: https://github.com/topoteretes/cognee-integrations/compare/codex-v1.0.3-local...HEAD
[1.0.3-local]: https://github.com/topoteretes/cognee-integrations/releases/tag/codex-v1.0.3-local
[1.0.0-local]: https://github.com/topoteretes/cognee-integrations/releases/tag/codex-v1.0.0-local
