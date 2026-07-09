# Changelog

All notable changes to the Cognee Memory plugin for Claude Code are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-06-27

Initial public release of the Cognee Memory plugin for Claude Code. The notes
below are seeded from the project's git history.

### Added

- Claude Code plugin built on the Cognee V2 API, with session hooks and agent identity.
- Session memory capture for prompts, tool traces, and assistant responses, plus context injection on prompt submit and graph sync on session end.
- Cognee as the default memory over the native `MEMORY.md`.
- Background `remember` and `cognify` status polling so the graph build runs without blocking the session.
- Resilient recall client with a circuit breaker and bounded timeouts; HTTP-first lookups that no longer trust empty CLI output.
- Automatic Cognee installation and the Claude Code marketplace entry.
- Automatic local API server handling with a separate, non-blocking bootstrap process.
- `cognee-statusline.sh` with automatic status line setup and a per-turn audit log.
- Typed memory entries, an idle watcher, and auto-improve.
- Standardized Cognee session names as `{agent}_{native_session_id}`.

### Changed

- Use datasets instead of sessions as the primary point of contact with agents; removed session switching and ad-hoc agent creation.
- Renamed `service` config to `base_url`.
- Tightened the over-budget poll and made the bridge return a uniform `parse_error` shape.
- Pinned the Cognee dependency to a known-good version.

### Fixed

- Enforce an overall poll deadline, retry on parse errors, and return uniform error shapes.
- Catch network and HTTP errors in the bridge POST and apply a spin-loop floor.
- Resolve the SSL certificate issue on local connections.
- Make pending prompts and the bridge cache concurrency-safe; fix concurrent startup races with lazy bootstrap.
- Fix database setup for fresh environments.
- Fix persistence of duplicated files and the graph-context exit/compaction hooks.

[Unreleased]: https://github.com/topoteretes/cognee-integrations/compare/claude-code-v0.2.0...HEAD
[0.2.0]: https://github.com/topoteretes/cognee-integrations/releases/tag/claude-code-v0.2.0
