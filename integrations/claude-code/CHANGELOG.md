# Changelog

All notable changes to the **Cognee Plugin for Claude Code** (`cognee-memory`) are
documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versions are tagged `claude-code-v<version>` (see [RELEASING.md](./RELEASING.md)).

## [Unreleased]

## [0.2.0]

### Added
- Background `remember` + `cognify` status polling so session capture never blocks the turn.
- Standardized Cognee session names to `{agent}_{native_session_id}`.
- Local Cognee API server support for Claude Code / Codex (proxy mode via `API_URL`).
- Automatic Cognee installation on plugin setup, plus marketplace registration.
- Prefer Cognee as the default memory over native `MEMORY.md`.

### Changed
- HTTP-first bridge with a non-blocking background path.
- Tightened the over-budget poll and gave `parse_error` a uniform shape.

### Fixed
- Bridge `POST` now catches network and HTTP errors instead of failing the hook.
- Overall poll deadline + parse-error retry; spin-loop floor to avoid busy-waiting.
- Recall reaches the graph scope; per-turn audit log.
- SSL certificate handling for fresh environments.

## [0.1.0]

### Added
- Initial Claude Code plugin: V2 API, session hooks, and identity (2026-04-10).
- Typed entries, idle watcher, and auto-improve loop.

[Unreleased]: https://github.com/topoteretes/cognee-integrations/compare/claude-code-v0.2.0...HEAD
[0.2.0]: https://github.com/topoteretes/cognee-integrations/releases/tag/claude-code-v0.2.0
[0.1.0]: https://github.com/topoteretes/cognee-integrations/releases/tag/claude-code-v0.1.0
