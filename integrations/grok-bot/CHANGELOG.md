# Changelog

All notable changes to the **cognee-grok-bot** plugin are documented here.

The version here must match the `version` field in `.cursor-plugin/plugin.json`.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.3]

### Fixed
- `.cursor-plugin/plugin.json` lists the rule file in `rules`
  (`./rules/cognee-memory.mdc`). The manifest had no `rules` field and relied on
  discovery of the `rules/` folder, and Grok Bot did not appear to load the rule:
  the bot did not follow it.

## [0.1.2]

### Changed
- The Cognee server is declared inline in `.cursor-plugin/plugin.json`
  (`mcpServers.cognee.url`) instead of in a separate `mcp.json`, which is removed,
  so Grok Bot does not have to resolve a file path to find the server.
- The plugin description ends with its version, so the installed version is
  visible in Grok Bot's plugin list.

## [0.1.1]

### Fixed
- `.cursor-plugin/plugin.json` now names `mcp.json` in `mcpServers`. Grok Bot did
  not pick up the Cognee server from the root `mcp.json` on its own, so after
  install the server showed no availability and no Cognee sign-in appeared.

## [0.1.0]

### Added
- Cursor-format plugin for Grok Bot. `mcp.json` attaches the hosted Cognee MCP
  server at `https://mcp.aws.cognee.ai/mcp`; Grok Bot signs the user in to Cognee
  with OAuth, so there is no API key to paste and nothing to install.
- Always-on rule `rules/cognee-memory.mdc` that makes Cognee the bot's memory:
  recall before answering, remember durable facts, always name the dataset, and
  confirm before deleting. It plays the part of the Claude Code plugin's
  SessionStart memory steer, since Grok Bot runs no plugin hooks.
- A line after each answer that used Cognee: `> 🧠 Cognee memory (<dataset>): …` for
  recalled memory the answer used, and `> 🧠 Saved to Cognee (<dataset>): …` for what
  was stored, like the Claude Code plugin's credit line.
- Root `.cursor-plugin/marketplace.json` listing the plugin, so the repository can
  be added as a marketplace in Grok Bot or Cursor.
