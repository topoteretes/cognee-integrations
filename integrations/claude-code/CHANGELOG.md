# Changelog

All notable changes to the **cognee-memory** Claude Code plugin are documented here.

The version here must match the `version` field in both `.claude-plugin/plugin.json`
and the plugin's entry in the repository-root `.claude-plugin/marketplace.json` — Claude
Code only offers an update when that string changes. Tag releases as
`cognee-memory-vX.Y.Z` (matching the repo's per-plugin tag convention).

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [1.2.0]

### Added
- **One-time configuration via `~/.cognee/.env`.** API keys and URLs
  (`COGNEE_BASE_URL`, `COGNEE_API_KEY`, `LLM_API_KEY`, and every other env var the
  plugin reads) no longer need to be exported in each shell: values placed in
  `~/.cognee/.env` — the durable cognee home shared with the Codex plugin — are
  injected into the environment at process start with setdefault semantics, so a
  real shell export still wins per terminal, and spawned processes (local server,
  watchers) inherit them unchanged. The file accepts pasted `export KEY=value`
  lines, is created with a commented template (mode `0600`) on first session
  start, and its location can be overridden with `COGNEE_ENV_FILE`. `doctor.py`
  gained an **Env File** row showing which keys the file defines (names only,
  never values) and which are overridden by shell exports. The parser tolerates
  Windows-written files: a UTF-8 BOM (PowerShell 5.1) is stripped, UTF-16 (a PS5
  `>` redirect) is decoded, and CRLF line endings are handled — so copy/paste
  setup blocks work from any shell.

## [1.1.1]

### Fixed
- **The "update available" nudge now clears as soon as the update is applied.** The
  marker is a snapshot from the background check, and nothing rewrote it when the
  plugin actually updated — so the status line kept advertising an update that was
  already installed until the next check. Both surfaces now compare the marker's
  `installed_version` against the version *running* and suppress the nudge on a
  mismatch, so the status line clears on its next refresh (~2s) instead of up to an
  interval later. The comparison is against the running version, not the newest copy
  on disk, so a background auto-update that a session has not reloaded yet correctly
  keeps nudging.

### Changed
- **Pinned cognee version is now `1.4.0`** (was `1.2.2.dev3`). The plugin installs
  this into its own managed venv on session start, so existing installs pick it up
  on the next session.
- **Update check now runs at most hourly instead of daily.**
  `COGNEE_UPDATE_CHECK_INTERVAL` defaults to `3600` (was `86400`). The check is a
  conditional `If-None-Match` request, so the steady state is a 304 with an empty
  body — cheap enough that a published release gets noticed within the hour rather
  than after up to a day. Still bounded regardless of how often the idle watcher
  relaunches, and still opt-out via `COGNEE_UPDATE_CHECK=off`.

## [1.1.0]

Status-line release. The line now says whether memory is actually working, which
server it is talking to, and what it just did — answered **per terminal**, since two
sessions on one machine can legitimately disagree.

```
● cognee: agent_sessions · local · recall 4s/5t/0g/1a · saved 2p/41t/2a
│                          │       └ what memory did this turn
│                          └ bold cyan = local, bold magenta = cloud
└ green ● healthy · red ✕ (reason) when not — connection and LLM key share this slot
```

### Added
- **Server-connection glyph, colour-coded.** A bold green `●` once the server is
  confirmed up **and** authenticated; on failure a bold red `✕ (<reason>)` with the
  reason inside the colour, so the verdict reads as one unit —
  `incorrect_cognee_api_key` for a missing, wrong, or expired `COGNEE_API_KEY`,
  `unreachable` for a server that is down or dies mid-session, or `server_error` for a
  5xx. Recorded by the hooks that already talk to the server, so the line stays green
  until a failure is actually observed and clears on the next success. A cold start
  still migrating stays silent rather than flashing a false red. Read from local
  markers only — no network on refresh.
- **Local-mode `LLM_API_KEY` health, in that same slot.** A bold red
  `✕ (incorrect_llm_api_key)` when no key is configured anywhere the server would
  look, or when the provider rejects the one that is — one reason for both, because
  the fix is the same either way (`llm-state.json` still records which it was). The
  two failure classes are told apart by the reason rather than by colour:
  `incorrect_cognee_api_key` is the key this plugin uses to reach the server,
  `incorrect_llm_api_key` is the key the local server uses to reach the LLM. An
  LLM-key failure *replaces* the `●` rather than sitting beside it, and a
  server-connection failure outranks it — if the server can't be reached, its LLM key
  is not the actionable problem. The key is resolved exactly as
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
  wins, except that a fresher **server-wide** failure in the shared marker takes
  precedence (`unreachable` / `server_error`), because the server really is shared
  and a just-observed outage applies to everyone. `incorrect_cognee_api_key` is *not*
  propagated — it describes the other session's credential rather than the server, so
  a keyless cloud terminal starting up cannot turn a healthy local one red. Nor does a
  fresher shared `ready` clear your own failure: another terminal's working key says
  nothing about yours.
- **Recall counts at the end of the line.** `· recall 4s/5t/0g/1a · saved 2p/41t/2a`
  — `recall` is what this turn's lookup found (`s`ession turns, `t`races, `g`raph
  context, `a`gent guidance), `saved` is what the previous turn persisted
  (`p`rompts, `t`races, `a`nswers). The same numbers the Codex plugin injects into
  model context, rendered faint here so they stay secondary. Read from a marker the
  prompt hook already wrote, so the renderer stays network-free.
- **The mode stands out** — `local` in bold cyan, `cloud` in bold magenta. It is the
  one field worth a double-take, since it says which memory you are about to write
  to; red and green are left to the health glyph, amber to the update nudge.
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
- **The per-prompt readiness gate now prefers an authenticated probe**, so a bad or
  expired key is classified as `incorrect_cognee_api_key` instead of being masked as
  healthy by an unauthenticated `/health` 200 — and recall skips the turn rather than
  attempting against a backend that will reject it. Falls back to `/health` when the
  authed probe can't classify (no key, or an older server without the endpoint).
- **The status line now resolves its own server URL** instead of leaving it empty when
  nothing is configured, mirroring the hooks' resolution exactly
  (`COGNEE_LOCAL_API_URL` → `COGNEE_BASE_URL` → config file → `http://localhost:8011`).
  A marker is only trusted when its `base_url` matches this session's; with no URL of
  our own that check could never fire, so a record written for a different server —
  another terminal's cloud tenant, say — was accepted by a local session. This is what
  gives that guard teeth in the default local setup, where nothing is exported.
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
