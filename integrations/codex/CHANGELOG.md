# Changelog

All notable changes to the Cognee Codex plugin are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.3-local] - 2026-06-24

### Added
- CLI-first Cognee workflows for memory, knowledge graphs, codebase ingestion, and UI launch
- Automatic session memory capture via Codex hooks (SessionStart, UserPromptSubmit, PostToolUse, Stop)
- Context injection from session and graph memory on every prompt
- Session-to-graph sync on session end
- Local Cognee API bootstrap mode
- Cloud/remote managed endpoint mode
- Cognee preferred memory steering

### Changed
- Version consistency enforced across inventory.yml and plugin.json

## [1.0.0] - 2026-06-01

### Added
- Initial Codex plugin with Cognee CLI skills
- Setup, memory, codebase ingestion, and UI launch commands
