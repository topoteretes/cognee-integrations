# Changelog

All notable changes to the **cognee** Codex CLI plugin are documented here.

The version here matches the `version` field in `.codex-plugin/plugin.json`. Note
the `cognee` marketplace is `git-subdir`-pinned to `main`, so updates are actually
delivered per-commit via `codex plugin marketplace upgrade cognee` — this `version`
is the cache key and semver record, bumped on each release, not the update trigger.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.0]

Bundles the arc since the automatic install/server-bootstrap work. Shares most
changes with the Claude Code plugin; Claude-only items (native `MEMORY.md`
memory-preference steer, `settings.json` status-line self-eviction) do not apply
to Codex, which renders its status inline.

### Added
- **Automatic Cognee installation + server bootstrap** for Codex, with the Codex
  marketplace. Uses the same self-managed `uv` virtualenv (`~/.cognee-plugin/venv`)
  and data pinned to `~/.cognee`, shared with the Claude Code plugin.
- **Lazy bootstrap.** SessionStart spawns a detached worker to boot the local
  server (including DB migrations) so the hook returns fast.
- **Server-first recall client** with a circuit breaker and bounded timeouts;
  falls back to the CLI only on a genuine failure, never on an empty result.
- **Background remember + cognify status polling.** Writes enqueue and poll to
  completion instead of holding one request open past the cloud's request ceiling.

### Changed
- **Single-principal / session-id model.** Session IDs are the point of contact
  with agents; removed per-agent user creation.
- **Dataset-scoped model.** Removed session switching in favor of datasets, with
  one shared default dataset (`agent_sessions`) across the Codex and Claude plugins.
- **Deterministic session naming** as `{agent}_{host_session_id}`.
- **Session→graph sync via session-aware `improve`**, replacing full-transcript
  re-cognify on every sync; the legacy document bridge remains as a fallback.
- **New session distillation logic.**
- **Cloud mode is now a pure thin client.** The cloud/remote setup path (health
  check, `/users/me`, dataset ensure, default-user key mint) uses stdlib `urllib`
  instead of `aiohttp`, so connecting to Cognee Cloud no longer requires the
  plugin's local virtualenv.
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

## [1.0.3]

- Baseline: session-aware capture (session starts, user prompts, tool results,
  assistant stops) into Cognee session memory, recall on each prompt, inline
  status visibility, and local and Cognee Cloud modes.
