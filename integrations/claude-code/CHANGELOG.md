# Changelog

All notable changes to the **cognee-memory** Claude Code plugin are documented here.

The version here must match the `version` field in both `.claude-plugin/plugin.json`
and the plugin's entry in the repository-root `.claude-plugin/marketplace.json` — Claude
Code only offers an update when that string changes. Tag releases as
`cognee-memory-vX.Y.Z` (matching the repo's per-plugin tag convention).

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0]

First release under formal semantic versioning — marks the official start of
versioning for this plugin. Supersedes the unversioned `0.2.0` baseline and
bundles all changes since, from the automatic install/server-bootstrap work
onward.

### Added
- **Automatic Cognee installation + server bootstrap.** A self-managed,
  `uv`-provisioned virtualenv under `~/.cognee-plugin/venv`, with data pinned to
  `~/.cognee` so it survives venv rebuilds and cognee upgrades.
- **Lazy bootstrap.** SessionStart spawns a detached worker to boot the local
  server (including DB migrations), so the hook returns fast and never times out.
- **Automatic status-line setup.** The plugin writes/enables its status line into
  `~/.claude/settings.json` on first run — no manual configuration.
- **Server-first recall client** with a circuit breaker and bounded timeouts;
  falls back to the CLI only on a genuine failure, never on an empty result.
- **Background remember + cognify status polling.** Writes enqueue and poll to
  completion instead of holding one request open past the cloud's request ceiling.
- **Memory-preference steer.** SessionStart asserts Cognee as the preferred
  memory over Claude Code's native `MEMORY.md` (opt out with `COGNEE_PREFER_MEMORY`).
- **Status-line cleanup on uninstall/disable.** The renderer self-evicts its
  `statusLine` entry from `~/.claude/settings.json` when the plugin is no longer
  enabled, and the entry is written as an existence-guarded command so an
  uninstalled plugin never leaves a broken status-line command behind.

### Changed
- **Single-principal / session-id model.** Session IDs are the point of contact
  with agents; removed per-agent user creation.
- **Dataset-scoped model.** Removed session switching in favor of datasets, with
  one shared default dataset (`agent_sessions`) across the Claude and Codex plugins.
- **Deterministic session naming** as `{agent}_{host_session_id}`.
- **Session→graph sync via session-aware `improve`**, replacing full-transcript
  re-cognify on every sync; the legacy document bridge remains as a fallback.
- **New session distillation logic.**
- **Cloud mode is now a pure thin client.** The cloud/remote setup path (health
  check, `/users/me`, dataset ensure, default-user key mint) uses stdlib `urllib`
  instead of `aiohttp`, so connecting to Cognee Cloud no longer requires the
  plugin's local virtualenv. The venv is now built only in local mode.
- Renamed the `service_url` config/env to `base_url`.

### Fixed
- **TLS certificate verification for cloud/HTTPS.** All `urllib` HTTPS calls now
  share a certifi-backed SSL context (falling back to `SSL_CERT_FILE` / system
  cert bundles), fixing `CERTIFICATE_VERIFY_FAILED` against Cognee Cloud on macOS
  Python builds that lack root CAs in the default context.
- Concurrency-safe pending-prompt and bridge buffers (per-session files, no
  lost-update races).
- `base_url` handling and connecting to an existing dataset; dataset name/config
  resolution; `/users/me` identity resolution; and bridge-POST network/HTTP-error
  handling with bounded poll deadlines.

## [0.2.0]

- Baseline release: session-aware capture (prompts, tool traces, assistant
  responses), auto-routing recall on prompt submit, session→graph sync on
  session end, local and Cognee Cloud modes, and automatic Cognee bootstrap for
  local mode.
