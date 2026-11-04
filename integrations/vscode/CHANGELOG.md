# Changelog

All notable changes to the Cognee VS Code extension are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.1] - 2026-09-29

### Fixed
- Remembering a note, selection or file no longer fails after the first one on cognee >= 1.6.0,
  which treats an upload's filename as its identity in the dataset and refuses (409) a known name
  with different content. Notes were all uploaded as `note.md`, and two files sharing a basename
  collided. Uploads are now named `<name>-<sha256[:32]>.<ext>`, keeping the extension, and
  citations strip the suffix so they still resolve to the original file.
  Refs [#444](https://github.com/topoteretes/cognee-integrations/issues/444).

## [0.1.0] - 2026-07-23

### Added
- Editor-agnostic core (`src/core`) with a typed Cognee HTTP client, deterministic
  per-repository dataset scoping, configuration validation, and Evidence/citation parsing.
- Commands: **Ask My Project Memory**, **Recall**, **Remember Selection**, **Remember File**,
  **Index Workspace**, **Forget Project Memory**, and **Set Up**.
- "Ask my project memory" webview panel that renders answers with clickable citations to source files.
- Direct citation resolution via a per-workspace path index: files you remember are recorded with
  their exact relative path, so a citation resolves straight to the file you ingested — even when the
  workspace has several files of that name. Falls back to snippet-content matching, then a user pick.
- Unit tests running against a mocked Cognee backend (no live keys, no LLM in CI).
