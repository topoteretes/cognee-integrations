# Changelog

All notable changes to the **cognee-memory** Claude Code plugin are documented here.

The version here must match the `version` field in both `.claude-plugin/plugin.json`
and the plugin's entry in the repository-root `.claude-plugin/marketplace.json` — Claude
Code only offers an update when that string changes. Tag releases as
`cognee-memory-vX.Y.Z` (matching the repo's per-plugin tag convention).

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.0]

Status-line release. The line now says whether memory is actually working, which
server it is talking to, and what it just did — answered **per terminal**, since two
sessions on one machine can legitimately disagree.

```
● cognee: agent_sessions · local · recall 4s/5t/0g/1a · saved 2p/41t/2a
│                          │       └ what memory did this turn
│                          └ bold cyan = local, bold magenta = cloud
└ connection + LLM-key health share one slot
```

### Added
- **Server-connection glyph.** `●` once the server is confirmed up **and**
  authenticated; on failure `✕ (auth_failed)` for a wrong/expired `COGNEE_API_KEY`,
  `✕ (unreachable)` for a server that is down or dies mid-session, or
  `✕ (server_error)` for a 5xx. Recorded by the hooks that already talk to the
  server, so the line stays green until a failure is actually observed and clears on
  the next success. A cold start still migrating stays silent rather than flashing a
  false red. Read from local markers only — no network on refresh.
- **Local-mode `LLM_API_KEY` health, in that same slot.** `✕ (llm_no_key)` when no
  key is configured anywhere the server would look, `✕ (llm_auth_failed)` when the
  provider rejects it. An LLM-key failure *replaces* the `●` rather than sitting
  beside it, and a server-connection failure outranks it — if the server can't be
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
- **Per-terminal status.** Every signal answers for *this* terminal — one shell may
  have exported `LLM_API_KEY` while another didn't, or two may hold different
  `COGNEE_API_KEY`s against one server, and both now show the truth at once. Each
  writer keeps the machine-wide marker as **coordination** state (it gates recall and
  is shared with the Codex plugin, since both talk to one server on one port) plus a
  per-session copy — `conn-state/<session>.json`, `llm-state/<session>.json`,
  `recall/<session>.json` — as the **display** state the bar reads. Your own record
  wins, except that a *fresher failure* in the shared connection marker takes
  precedence, because the server really is shared and a just-observed outage applies
  to everyone; a fresher shared `ready` does **not** clear your own failure, since
  another terminal's working key says nothing about yours.
- **Recall counts at the end of the line.** `· recall 4s/5t/0g/1a · saved 2p/41t/2a`
  — `recall` is what this turn's lookup found (`s`ession turns, `t`races, `g`raph
  context, `a`gent guidance), `saved` is what the previous turn persisted
  (`p`rompts, `t`races, `a`nswers). The same numbers the Codex plugin injects into
  model context, rendered faint here so they stay secondary. Read from a marker the
  prompt hook already wrote, so the renderer stays network-free.
- **The mode stands out** — `local` in bold cyan, `cloud` in bold magenta. It is the
  one field worth a double-take, since it says which memory you are about to write
  to; red/green/amber are left to the health glyph and warnings.
- **Idle refresh.** The `statusLine` entry now sets `refreshInterval: 2`, so the bar
  keeps updating while a session sits idle. Without it Claude Code refreshes only on
  events, and a failure detected right after launch wouldn't surface until the next
  prompt.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_LLM_KEY_CHECK` | `true` | Background, provider-agnostic `LLM_API_KEY` validation (local mode) |
| `COGNEE_LLM_CHECK_INTERVAL` | `300` | Minimum seconds between LLM-key checks |
| `COGNEE_STATUSLINE_COUNTS` | `true` | Show the trailing `recall …/saved …` counts |
| `COGNEE_STATUSLINE_REFRESH_INTERVAL` | `2` | Status-line idle refresh in seconds; below `1` reverts to event-only |

### Changed
- **The per-prompt readiness gate now prefers an authenticated probe**, so a
  bad or expired key is classified as `auth_failed` instead of being masked as
  healthy by an unauthenticated `/health` 200 — and recall skips the turn rather
  than attempting against a backend that will reject it. Falls back to `/health`
  when the authed probe can't classify (no key, or an older server without the
  endpoint).
- **Documented two long-standing environment variables** that previously existed only
  in the source: `COGNEE_READY_PROBE_TIMEOUT` (the per-prompt readiness probe's
  timeout, default `1.0s`), plus a note naming the `COGNEE_*` variables that are the
  plugin's own inter-process plumbing — `COGNEE_USER_ID`, `COGNEE_SESSION_KEY`,
  `COGNEE_AGENT_SESSION_NAME`, `COGNEE_PLUGIN_IN_VENV`, `COGNEE_SYNC_*` — which are
  overwritten during startup and should not be set by hand. Neither is new in this
  release; both were simply undocumented.

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
