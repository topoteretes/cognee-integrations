# Changelog

All notable changes to the Cognee Claude Code plugin are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-06-29

### Added
- Status line shows installed plugin version (e.g. `· v0.2.0`)
- Observability: `elapsed_ms` added to `context_lookup_hit`, `context_lookup_empty`, `cognify_poll_*`, and `sync_bridge_done` hook log events
- Per-operation timeouts: `COGNEE_REMEMBER_TIMEOUT`, `COGNEE_REGISTER_TIMEOUT` env vars
- Cloud cold-start warmup ping on `SessionStart` (gated by `COGNEE_WARMUP`)
- Background remember with cognify status polling and `dataset_id` tracking
- `improve()` (memify) support for post-ingestion enrichment
- `cognee-plugin status` one-liner (runtime mode, URL, API key presence, version)
- "Cognee preferred memory" SessionStart steer instruction
- Circuit breaker for recall: threshold-based cooldown on repeated failures
- Session-context distillation via improve step

### Changed
- Remember API uses `run_in_background=true` by default (`COGNEE_REMEMBER_BACKGROUND`)
- Write timeouts no longer trigger CLI fallback (avoids duplicates)
- HTTP errors (4xx/5xx) from server are authoritative — no CLI fallback
- Recall routes through `_cognee_client.py` with breaker + timeout policy
- Runtime state resolution via API endpoints instead of local files
- Version consistency enforced across `inventory.yml`, `plugin.json`, `marketplace.json`

### Fixed
- Stale readiness marker no longer blocks recall after server restart
- Idle watcher double-write race on concurrent agent sessions
- Bridge poll no longer spins on zero interval
- SSL cert loading on macOS without certifi
- Server-first recall for cloud mode (removes local SDK dependency)

## [0.1.0] - 2026-06-01

### Added
- Initial Claude Code marketplace plugin
- Session-aware memory capture (prompt, trace, answer hooks)
- Context injection on every `UserPromptSubmit` via `session-context-lookup`
- Session-to-graph sync on `SessionEnd`
- Local Cognee API bootstrap mode
- Cloud/remote managed endpoint mode
- Status line: `cognee: <dataset> · <mode>`
- Idle watcher for periodic session persistence
- Configuration via env vars and `~/.cognee-plugin/claude-code/config.json`
