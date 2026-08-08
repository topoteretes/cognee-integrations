# Changelog

All notable changes to this repository will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project uses semantic versioning where practical.

## [Unreleased]

### Added
- Claude Code plugin status line now shows the installed plugin version.
- Claude Code observability logs now include `elapsed_ms` for recall, bridge, and improve paths.
- A release inventory consistency check that compares `integrations/inventory.yml` with installed plugin manifests.
- Repo release guidance for changelog and version bump workflow.

### Changed
- Cognee hook events in Claude Code are now normalized to a namespaced taxonomy.
- The Claude Code status line and hook scripts now prefer fail-silent local reads for version metadata.

## [0.2.0] - 2026-06-30

### Added
- Initial plugin manifests and release metadata for the Claude Code and Codex integrations.

### Changed
- Codex plugin status line now shows the installed plugin version.

### Fixed
- Inventory version metadata now matches the installed plugin manifests for Claude Code and Codex.

