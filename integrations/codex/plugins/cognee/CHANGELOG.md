# Changelog

All notable changes to the **cognee** Codex CLI plugin are documented here.

The version here matches the `version` field in `.codex-plugin/plugin.json`. Note
the `cognee` marketplace is `git-subdir`-pinned to `main`, so updates are actually
delivered per-commit via `codex plugin marketplace upgrade cognee` — this `version`
is the cache key and semver record, bumped on each release, not the update trigger.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [1.2.1]

### Changed
- **Pinned cognee version is now `1.4.0`** (was `1.2.2.dev3`). The plugin installs
  this into its own managed venv on session start, so existing installs pick it up
  on the next session.

## [1.2.0]

Status-visibility release. The status now says whether memory is actually working and
which server it is talking to, answered **per session**. Shares the connection and
LLM-key work with the Claude Code plugin; the Claude-only parts (recall counts in the
bar, bold-coloured mode, `settings.json` idle-refresh interval) do not apply here,
since Codex renders its status inline as plain text rather than in a terminal bar.

### Added
- **Server-connection glyph.** `●` once the server is confirmed up **and**
  authenticated; on failure `✕ (incorrect_cognee_api_key)` for a missing, wrong, or
  expired `COGNEE_API_KEY`, `✕ (unreachable)` for a server that is down or dies
  mid-session, or `✕ (server_error)` for a 5xx. Recorded by the hooks that already
  talk to the server, so it stays green until a failure is actually observed, and
  clears on the next success. A cold start still migrating stays silent rather than
  reporting a false failure. Read from local markers only — no network on refresh.
- **Local-mode `LLM_API_KEY` health, in that same slot.** `✕ (incorrect_llm_api_key)`
  when no key is configured anywhere the server would look, or when the provider
  rejects the one that is — one reason for both, because the fix is the same either
  way (`llm-state.json` still records which it was). An LLM-key failure *replaces* the
  `●` rather than sitting beside it, and a server-connection failure outranks it — if
  the server can't be
  reached, its LLM key is not the actionable problem. The key is resolved exactly as
  the server resolves it (Cognee's own config, so an env var, a `.env`, or Cognee's
  config file all count) and validated in the background idle watcher — never on the
  prompt path — with one `max_tokens=1` call through the same LLM stack Cognee uses.
  That makes it **provider-agnostic**: only `401`/`403` counts as an auth failure,
  any other response proves the key was accepted (including the `400` reasoning
  models return when one token is too few to finish a message), and a transport
  error with no HTTP status is inconclusive and leaves the previous verdict alone.
  Local mode only; verdicts expire after 30 minutes so a dead session's verdict never
  lingers.
- **Per-session status.** Every signal answers for *this* session — one shell may
  have exported `LLM_API_KEY` while another didn't, or two may hold different
  `COGNEE_API_KEY`s against one server, and both now report the truth at once. Each
  writer keeps the machine-wide marker as **coordination** state (it gates recall and
  is shared with the Claude Code plugin, since both talk to one server on one port)
  plus a per-session copy — `conn-state/<session>.json`, `llm-state/<session>.json` —
  as the **display** state the status reads. Your own record wins, except that a
  fresher **server-wide** failure in the shared marker takes precedence
  (`unreachable` / `server_error`), because the server really is shared and a
  just-observed outage applies to everyone. `incorrect_cognee_api_key` is not
  propagated — it describes the other session's credential, not the server; a fresher
  shared `ready` does **not** clear your own failure, since another session's working
  key says nothing about yours.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_LLM_KEY_CHECK` | `true` | Background, provider-agnostic `LLM_API_KEY` validation (local mode) |
| `COGNEE_LLM_CHECK_INTERVAL` | `300` | Minimum seconds between LLM-key checks |

### Changed
- **The per-prompt readiness gate now prefers an authenticated probe**, so a bad or
  expired key is classified as `incorrect_cognee_api_key` instead of being masked as
  healthy by an unauthenticated `/health` 200 — and recall skips the turn rather than
  attempting
  against a backend that will reject it. Falls back to `/health` when the authed
  probe can't classify (no key, or an older server without the endpoint).
- **The status now resolves its own server URL** instead of leaving it empty when
  nothing is configured, mirroring the hooks' resolution exactly
  (`COGNEE_LOCAL_API_URL` → `COGNEE_BASE_URL` → config file → `http://localhost:8011`).
  A marker is only trusted when its `base_url` matches this session's; with no URL of
  our own that check could never fire, so a record written for a different server was
  accepted by a local session.
- The status string stays deliberately **plain text** (no ANSI). Claude Code styles
  its bar; here the same string goes into the model's context, where escape sequences
  are only noise to read past. A regression test now guards this.
- **Documented two long-standing environment variables** that previously existed only
  in the source: `COGNEE_READY_PROBE_TIMEOUT` (the per-prompt readiness probe's
  timeout, default `1.0s`), plus a note naming the `COGNEE_*` variables that are the
  plugin's own inter-process plumbing — `COGNEE_USER_ID`, `COGNEE_SESSION_KEY`,
  `COGNEE_AGENT_SESSION_NAME`, `COGNEE_PLUGIN_IN_VENV`, `COGNEE_SYNC_*` — which are
  overwritten during startup and should not be set by hand. Neither is new in this
  release; both were simply undocumented.

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
