# Changelog

All notable changes to the **claude-code** integration will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-02

### Added

- **Cognee as the default memory backend**: The integration now prefers Cognee over Claude's native `MEMORY.md` for storing and retrieving session memory.
- **Background memory ingestion**: After a session, memory is written to the Cognee graph in a background task so the agent is never blocked waiting for ingestion to complete.
- **Cognify status polling**: The integration polls for background `cognify` job completion, giving users visibility into when their memory graph is ready.
- **Session distillation**: Integrated new session distillation logic to summarise and persist session knowledge into Cognee.
- **Standardised session naming**: Cognee session names now follow the `{agent}_{native_session_id}` convention for consistency across integrations.
- **Resilient recall client with circuit breaker**: The recall skill uses an HTTP-first strategy with a circuit breaker and bounded timeouts, falling back gracefully when the server is unavailable.
- **`auto_feedback` environment variable**: Added `AUTO_FEEDBACK` env var (default: `true`) to control automated feedback behaviour without code changes.
- **SSL certificate handling**: Added automatic resolution of SSL certificate issues for outbound requests from within the bridge.

### Changed

- **Recall strategy**: The recall skill now queries the Cognee HTTP server first instead of the local CLI, and no longer falls back to CLI when the server returns an authoritative HTTP error.
- **Unified error shape**: `parse_error` responses now use a consistent shape across all code paths, making error handling predictable for callers.
- **Over-budget poll tightened**: The poll loop for over-budget detection now enforces a strict overall deadline and includes a minimum spin-loop floor to avoid busy-waiting.
- **Session ID handling**: The native session ID is now sanitised and wrapped consistently before being passed to Cognee, matching the format expected by the session naming convention.
- **`remember` and `sync` skills refactored**: Directory structure split to mirror the `search` skill layout, improving maintainability.
- **Cognee version pinned**: Pinned the Cognee dependency to `1.2.2.dev0` for reproducible installs.

### Fixed

- **Network errors in bridge POST**: Unhandled network-level exceptions in the bridge POST handler are now caught and returned as structured error responses.
- **HTTP errors in bridge POST**: Non-2xx responses from the bridge endpoint are now detected and surfaced rather than silently ignored.
- **Background graph build write timeouts**: Write timeouts during background graph construction are no longer misclassified as the Cognee server being unreachable.
- **Recall dataset targeting**: Fixed a bug where the recall skill was hitting the wrong dataset when multiple datasets existed.
- **HTTP errors treated as non-authoritative in recall**: Corrected recall fallback logic so that transient HTTP errors do not permanently prevent the local CLI from being used.
- **Overall poll deadline for bridge requests**: Added a hard deadline on the polling loop so it cannot run indefinitely when the upstream service is unresponsive.
- **Parse-error retry behaviour**: The bridge now retries on parse errors with correct backoff instead of immediately surfacing the error to the caller.
