# Changelog

All notable changes to the Cognee Antigravity plugin are documented in this file.
The version must match `plugin.json` so Antigravity can identify the installed
package version.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [1.4.3]

### Added

- Native Antigravity package metadata, four named hooks, and Cognee skills.
- A bounded transcript adapter that reads only the final 1 MiB of JSONL to map
  Antigravity invocations, tool output, and completed responses into Cognee memory
  events.
- Plugin-specific backend selection through `COGNEE_ANTIGRAVITY_BACKEND`, shared
  `~/.cognee/.env` configuration, and private hook state under
  `~/.cognee-plugin/antigravity/`.

### Changed

- Align the shared runtime with current Claude Code and Codex: provider extras,
  code-graph indexing, dataset-aware sync, bounded logs, stale-state cleanup,
  recall accounting, and persistent improve cooldowns.
- Remove legacy config-file routing and full-transcript sync fallbacks.
- Support documented `executionNum` Stop payloads, deduplicate retried tool
  steps, and correlate out-of-order tool results by their call identity.
- Renew bootstrap ownership when a conversation resumes after its host exits.

### Safety

- Installing with `agy plugin install` never edits Antigravity settings; the plugin
  is registered through its native manifest and hook declarations.
