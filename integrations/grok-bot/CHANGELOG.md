# Changelog

All notable changes to the **cognee-memory** Grok plugin are documented here.

The version here must match the `version` field in `.grok-plugin/plugin.json`,
`.cursor-plugin/plugin.json`, and the plugin's entry in the repository-root
`.grok-plugin/marketplace.json`. Tag releases as `grok-bot-vX.Y.Z` (matching the
repo's per-plugin tag convention).

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0]

### Added
- **Initial Grok plugin**, ported from the Claude Code plugin (1.4.3). Grok Build
  loads plugins in the same layout Claude Code uses, so the hook scripts, skills
  and recall agent carry over with host-specific constants swapped: state under
  `~/.cognee-plugin/grok-bot/`, agent name `grok-bot-agent`, session prefix
  `grok`, per-plugin mode switch `COGNEE_GROK_BACKEND`, and the plugin root read
  from `GROK_PLUGIN_ROOT` (with `CLAUDE_PLUGIN_ROOT` as a fallback).
- **Grok-native hook payloads.** Grok Build documents camelCase hook fields
  (`hookEventName`, `sessionId`, `toolName`, `toolInput`, `workspaceRoot`); every
  hook now normalizes those onto the snake_case names the shared scripts read, and
  falls back to `GROK_WORKSPACE_ROOT` when the payload carries no `cwd`.
- **Explicit hook timeouts.** Grok's default hook timeout is 5 seconds, so every
  entry in `hooks/hooks.json` declares its own.
- **Grok Bot (cloud) path.** A `.cursor-plugin/plugin.json` manifest plus
  `mcp.json` point the Bot at a public `cognee-mcp` HTTP endpoint
  (`COGNEE_MCP_URL`, optional `COGNEE_API_KEY`), since Grok Bot only attaches
  remote MCP servers. The remember/search/forget skills describe the MCP tools to
  use in that mode.
- Root `.grok-plugin/marketplace.json` so `grok plugin marketplace add
  topoteretes/cognee-integrations` finds the plugin.

### Changed (relative to the Claude Code plugin)
- No `statusLine` is written into host settings: Grok Build keeps its
  configuration in `~/.grok/config.toml` and documents no status-line hook. The
  renderer still ships (`scripts/cognee-statusline.sh`) for manual use, and the
  per-prompt `Cognee memory: …` header carries the connection state in-context.
- The status-line renderer's self-eviction only fires when a settings scope
  explicitly lists the plugin as disabled; with no `enabledPlugins` map anywhere
  (Grok's normal state) it renders.
- Update checks read `~/.grok/plugins/known_marketplaces.json` and the published
  `.grok-plugin/marketplace.json`; the nudge suggests `grok plugin update`.
- The recall agent no longer pins a Claude model name.
