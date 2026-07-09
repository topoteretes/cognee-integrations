# Changelog

All notable changes to the **codex** integration will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-02

### Added

- **Dataset-based memory storage**: Replaced session switching with a dataset-per-agent model, using a single shared dataset by default (consistent with the Claude Code integration).
- **Session distillation**: Integrated new session distillation logic to summarise and persist session knowledge into Cognee.
- **Standardised session naming**: Cognee session names now follow the `{agent}_{native_session_id}` convention for consistency across integrations.
- **Resilient recall client with circuit breaker**: The recall skill uses an HTTP-first strategy with a circuit breaker and bounded timeouts, falling back gracefully when the server is unavailable.
- **`auto_feedback` environment variable**: Added `AUTO_FEEDBACK` env var (default: `true`) to control automated feedback behaviour without code changes.
- **SSL certificate handling**: Added automatic resolution of SSL certificate issues for outbound requests.

### Changed

- **Session management removed**: Session switching logic has been removed; the integration now uses datasets directly, which simplifies state management and avoids conflicts when multiple agents share Cognee.
- **Unified default dataset**: Both Codex and Claude Code now default to the same single dataset, ensuring cross-agent memory is shared out of the box.
- **Recall strategy**: The recall skill now queries the Cognee HTTP server first instead of the local CLI, and no longer falls back to CLI when the server returns an authoritative HTTP error.
- **`remember` and `sync` skills refactored**: Directory structure split to mirror the `search` skill layout, improving maintainability.
- **Status line setup**: Automatic status line initialisation is now configured during startup, reducing required manual setup steps.
- **Cognee version pinned**: Pinned the Cognee dependency to `1.2.2.dev0` (previously `1.2.1`) for reproducible installs.
- **`base_url` renamed from `service`**: The configuration key for the Cognee server address has been renamed from `service` to `base_url` for clarity.

### Fixed

- **Background graph build write timeouts**: Write timeouts during background graph construction are no longer misclassified as the Cognee server being unreachable.
- **Recall dataset targeting**: Fixed a bug where the recall skill was hitting the wrong dataset when multiple datasets existed.
- **HTTP errors treated as non-authoritative in recall**: Corrected recall fallback logic so that transient HTTP errors do not permanently prevent the local CLI from being used.
- **`base_url` bug**: Fixed a crash caused by incorrect resolution of the server base URL when connecting to an existing dataset.
- **Connecting to an existing dataset**: Resolved issues that prevented the integration from attaching to an already-initialised Codex dataset on startup.
- **New session behaviour**: Fixed several edge cases in how new Codex sessions were initialised and handed off to Cognee.
