# Changelog

All notable changes to the Cognee Codex plugin are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.3-local] - 2026-06-25

Initial public release of the Cognee Codex plugin. The notes below are seeded
from the project's git history.

### Added

- Codex CLI plugin that captures session memory: prompts, tool traces, and assistant responses.
- Context injection on prompt submit and graph sync on session end.
- Automatic installation and the Codex marketplace entry.
- Automatic local API server handling: start the server on session start, kill it on session end, with a separate non-blocking bootstrap process.
- Cloud connection support with an API-endpoint-compatible Codex client.
- Multiple agent sessions, including reuse of the same agent name across sessions.
- Per-session exit watcher and session-distillation logic.
- Resilient recall client with a circuit breaker and bounded timeouts; HTTP-first lookups that no longer trust empty CLI output.
- `auto_feedback` environment variable (defaults to true).
- Standardized Cognee session names as `{agent}_{native_session_id}`.

### Changed

- Use datasets instead of sessions as the primary point of contact with agents; removed session switching and ad-hoc agent creation.
- Default to a single dataset shared across Claude and Codex.
- Renamed `service` config to `base_url`.
- Pinned the Cognee dependency to a known-good version.

### Fixed

- Resolve the SSL certificate issue on local connections.
- Make pending prompts and the bridge cache concurrency-safe; fix concurrent startup races with lazy bootstrap.
- Recreate the agent correctly when the API key is stale.
- Fix mode resolution and connecting to an existing dataset.
- Fix recall dataset targeting and the `base_url` resolution bug.
- Background graph build that no longer treats write timeouts as unreachable.

[Unreleased]: https://github.com/topoteretes/cognee-integrations/compare/codex-v1.0.3-local...HEAD
[1.0.3-local]: https://github.com/topoteretes/cognee-integrations/releases/tag/codex-v1.0.3-local
