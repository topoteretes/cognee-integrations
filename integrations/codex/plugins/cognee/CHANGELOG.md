# Changelog

All notable changes to the **cognee** Codex CLI plugin are documented here.

The version here matches the `version` field in `.codex-plugin/plugin.json`. Note
the `cognee` marketplace is `git-subdir`-pinned to `main`, so updates are actually
delivered per-commit via `codex plugin marketplace upgrade cognee` — this `version`
is the cache key and semver record, bumped on each release, not the update trigger.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [1.5.0]

### Added
- **`cognee-forget` skill — user-directed deletion of memory.** "Forget what we
  talked about tennis" now has a first-class guided flow (ported from the
  Claude Code plugin): the agent syncs the live session (so unsynced content
  becomes a deletable document), lists the plugin dataset, judges candidate
  documents by their raw content, confirms with the user, and deletes each
  match via `POST /api/v1/forget`. Documents from the same session are treated
  as a group — deleting one while keeping its siblings would leave the topic
  recallable. The `memory` skill's Forget section now routes to this flow; the
  bare `cognee-cli forget` commands remain as the server-unreachable fallback.
- **`scripts/cognee-forget.sh`** — the skill's server access. Subcommands
  (`sync`, `datasets`, `data`, `raw`, `forget`, `env`) each resolve credentials
  per invocation the same way the other wrappers do (shell env →
  `~/.cognee/.env` → the auto-minted `api_key.json` at the shared plugin root).
  Every API command appends a final `HTTP <status>` line; with no key
  resolvable the helper exits 2 with guidance instead of sending a request that
  can only 401. Single-document deletion only — dataset-wide and `everything`
  scopes stay behind an explicit-user-request warning in the skill.
- **E2e coverage in the shared suite.** `tests/e2e/test_forget_script.py` runs
  the wrapper as a subprocess against the mock server for BOTH suites
  (credential resolution incl. the `api_key.json` fallback and the exit-2 path,
  payload shape, status trailer, 404 pass-through).

## [1.4.2]

### Fixed
- **A turn captured while the server is down is no longer lost.** Writes were
  buffered to the warmup spillway only when `server_usable()` already reported
  False. That check relies on a ready marker with a 30s TTL, so a server dying
  inside that window left it returning True: the write was attempted for real,
  it raised, and the `except` branch only logged `stop_store_error` — the entry
  was buffered nowhere and the turn was gone. Both the Stop (QA) and PostToolUse
  (trace) paths now buffer on failure and replay when the server returns.

  Failures that cannot succeed on replay are *not* buffered. A 4xx is dropped
  with `"buffered": false` in the log, because the drain stops at the first entry
  it cannot send and only removes what it drained — a permanently rejected entry
  would sit at the head of the queue and block everything behind it. Transport
  failures and 5xx are retried; new events: `store_buffered_after_error`,
  `trace_buffered_after_error`.

- **`improve` now reports whether the graph actually finished building.**
  `improve_session_via_http` submitted and returned `ok` without polling, so a
  caller could not tell an accepted bridge from a completed one — the graph could
  still be empty while the result said success. It now polls the cognify and
  memify pipelines (`COGNEE_IMPROVE_POLL_DEADLINE`, default 600s, split between
  the two) and reports `cognify_status` / `memify_status`. Best-effort: a poll
  that times out never turns a successful submit into a failure. This was the one
  piece of the background-remember work that did not travel with the rest.

## [1.4.1]

### Fixed
- **Boot points no longer mistake a busy server for an absent one.** The boot
  decision previously rested on a single 2s `/health` probe, so a server busy
  cognifying could miss the deadline, be declared absent, and have the venv
  upgraded and migrations run underneath it while it still held the graph
  store's file lock (2026-08-13 incident). Server presence is now judged from
  three evidence sources — the classified HTTP probe, the bare TCP handshake
  (a busy server still accepts it; a dead one refuses), and a server pidfile
  written at uvicorn spawn — and only a positively-absent verdict (refused
  port, no live server pid, confirmed by a delayed re-probe) licenses
  installing or booting. The verdict and its evidence are recorded on every
  `endpoint_mode_selected` decision, the license is re-verified after the
  (minutes-long) install and again under the boot lock, and a spawned server
  that dies before becoming healthy (e.g. lost port-bind race) is detected
  instead of being waited out.

## [1.4.0]

### Added
- **`COGNEE_BACKEND` per-terminal mode switch.** `~/.cognee/.env` may now hold
  the cloud vars (`COGNEE_BASE_URL`, `COGNEE_API_KEY`) *and* the local vars
  (`LLM_API_KEY`, …) together; with nothing exported, cloud wins as before.
  `export COGNEE_BACKEND=local` (or `=cloud`) flips a single terminal — the
  shared name switches both the Claude Code and Codex plugins at once, while
  `COGNEE_CODEX_BACKEND` targets this plugin only and beats the shared name.
- **Forced cloud is pinned, and misconfiguration is surfaced.** With
  `COGNEE_BACKEND=cloud` but no `COGNEE_BASE_URL`, the plugin no longer
  silently falls back to local (no local server boot, no venv build); the
  status line shows `✕ (missing_cognee_base_url)` and `doctor`'s mode row
  explains what forced the decision and what is missing.

### Fixed
- **`COGNEE_CODEX_BACKEND=local` now holds on the HTTP hot paths.** The switch
  used to clear the cloud URL only in `load_config()`'s view, while
  recall/remember read `COGNEE_BASE_URL` from the environment — where the env
  file had already injected the cloud URL — so those calls still went to the
  cloud. A forced-local switch now scrubs `COGNEE_BASE_URL`/`COGNEE_API_KEY`
  from the process environment itself (with empty strings, so re-running the
  loader in child processes cannot re-inject the file's values).

## [1.3.5]

### Changed
- **Pinned cognee version is now `1.4.2`** (was `1.4.0`). The plugin installs
  this into its own managed venv on session start, so existing installs pick it
  up on the next session. No plugin-side behavior changes: cognee 1.4.1+ resolves
  an *omitted* session id to a per-dataset default (`default_session_<dataset_id>`),
  but the plugin always sends its explicit per-session session id, which passes
  through unchanged.
- **Background remember + cognify status polling in the session→graph bridge**,
  ported from the claude-code plugin. The legacy document bridge
  (`persist_session_cache_to_graph_via_http`) previously POSTed to
  `/api/v1/remember` synchronously (`run_in_background=false`) with a 600s
  timeout — roughly the cloud's 10-minute NGINX request ceiling. A large
  cognify got abandoned mid-flight (504/HTML; the server still finishes), so
  the bridge wrongly read it as a failure and retried, duplicating work. The
  bridge now submits with `run_in_background=true` and polls
  `GET /api/v1/datasets/status` (`wait_for_cognify`) to completion, and marks
  the SHA256 dedup digest ONLY on completed/unknown — errored/timeout stay
  unmarked so the detached retry re-submits (no loss, no dup-on-success).
  Tunables registered in config: `COGNEE_BRIDGE_POLL_DEADLINE`,
  `COGNEE_BRIDGE_SUBMIT_TIMEOUT`, `COGNEE_COGNIFY_POLL_INTERVAL`,
  `COGNEE_STATUS_REQUEST_TIMEOUT`.
- **Explicit remember now confirms queryability**, also ported from the
  claude-code plugin. `cognee-remember.sh` submitted in the background but
  discarded the response, so it could never confirm completion — a recall
  right after "remember this" silently hit the not-yet-cognified graph, and
  an errored cognify was never surfaced. `_remember_http.py` now captures the
  enqueue handle (`dataset_id`, `pipeline_run_id`, `status`) and, by default,
  waits a bounded `COGNEE_REMEMBER_WAIT_SECONDS` (8s) polling
  `GET /api/v1/datasets/status`, adding `queryable`/`wait_outcome` to the
  result. Set `COGNEE_REMEMBER_WAIT_SECONDS=0` for fire-and-forget, or
  `COGNEE_REMEMBER_BACKGROUND=false` for a fully synchronous write. The
  memory skill documents the background + eventual-consistency semantics.

### Added
- **Pipeline-health warning in the status line.** The status injected into
  model context now leads with `⚠ N pipeline(s) stuck` or `⚠ server-down`
  when the external pipeline-health sweep has a fresh finding, matching the
  Claude Code bar. Reads the machine-wide, integration-neutral
  `~/.cognee-plugin/pipeline-health.json` the sweep already writes — plain
  text (no ANSI), never raises, and hides findings older than 30 minutes
  (a stale file means the sweep itself stopped, which this glyph does not
  monitor). Bare `warn` classifications stay silent per the notify policy;
  only `alert`/`critical` (or a down server) surface.

## [1.3.4]

### Added
- **Cloud credits in the status line.** Cloud sessions now show the
  connected tenant's balance right after the mode — `credits: $14.23`, plain
  text (`-$…` once negative) — followed by the approximate cost of the last
  memory operation, e.g. `· last turn ~$0.04`. Motivated by an incident where
  a tenant overshot its budget by ~$159 through the integration path with no
  client-side visibility at any point.
  - **Costs appear when the turn finishes, not one prompt later.** A dedicated
    `credits-refresh.py` hook on `Stop` diffs the tenant's spend counter
    against the turn-start baseline and attributes the delta as `turn`;
    explicit `remember` and `improve` operations are attributed at their own
    completion points. (Codex does not support async command hooks — an
    `async: true` hook is skipped entirely — so the refresh runs as a plain
    Stop entry with a 10s timeout; Codex launches matching hooks
    concurrently, so the QA store on the same event does not wait on it.)
    Costs carry a `~` on purpose: spend aggregates asynchronously and
    concurrent operations overlap, so the delta is an attribution, not an
    invoice. Most conversational turns genuinely cost ~$0 — recall runs with
    `only_context=true` (no LLM completion) — so the label typically moves on
    improve/remember, while the balance refreshes every turn.
  - **Multi-tenant correct.** The balance comes from the platform API's
    per-tenant spend records (`/api/v1/billing/credits/overview` on
    `COGNEE_PLATFORM_API_URL`, default `https://api.aws.cognee.ai` — the
    tenant data plane has no billing routes), selected by the tenant id the
    `connections/me` lookup already returns (zero extra calls). The marker
    (`credits.json`) is a map keyed by tenant id, so several terminals
    connected to different tenants each display their own balance, and one
    tenant's spend can never be misattributed to another's turn. Entries are
    written atomically under a lock (per-pid staging files), so concurrent
    refreshes can't tear the file or drop each other's tenants.
  - The status-line renderer stays pure-local (reads only the marker, 15-min
    staleness TTL). Strictly the connected tenant's budget or nothing: the
    segment hides entirely for local servers (no credits concept) and
    whenever the connected tenant cannot be determined — never another
    workspace's number, never the all-tenants aggregate. Refresh cadence: per turn (prompt + turn end)
    plus a session-long background poll (`COGNEE_CREDITS_CHECK_INTERVAL`,
    300s), so the balance stays fresh through long idle stretches. Opt
    out with `COGNEE_STATUSLINE_CREDITS=off`.

## [1.3.3]

### Fixed
- **False `✕ (unreachable)` in the status line.** Probe and recall
  timeouts were classified as "unreachable" and persisted into the shared
  connection state, so a busy-but-healthy server randomly turned the status red
  and skipped recall — in both local and cloud mode. Timeouts are now a
  no-verdict: transport failures are classified (connection refused / DNS →
  `unreachable`; timeout → keep prior state), and `unreachable` is only ever
  written on positive absence.
  - The recall attempt itself is now the health probe: a successful scope call
    marks ready, a refused connection marks `unreachable`, a 401/403 marks
    `auth_failed` (detected from the real request, remaining scopes skipped),
    and all-5xx marks `server_error`. The synthetic pre-recall probe survives
    only as a re-entry check while the marker holds a failure state.
  - The recall circuit breaker is keyed by `base_url` (cloud failures no
    longer red a local status, and vice versa — including across the Claude
    Code plugin, which shares the breaker file), counts failures in a sliding
    window instead of forever, re-arms half-open after cooldown, never counts
    timeouts, and the status line renders its real trip reason.
  - The renderer shows a ✕ only for fresh, definitive failures (30 min TTL);
    stale or ambiguous state renders no glyph.
  - Breaker state writes use tmp + atomic replace so readers never see a torn
    file.

### Added
- **`✕ (not_responding)` status** — distinct from `unreachable`: the server
  accepts connections but hasn't answered for N consecutive timeout-only
  prompts (default 3, `COGNEE_SLOW_STREAK_THRESHOLD`; streak window
  `COGNEE_SLOW_STREAK_WINDOW`, 600s). A single slow response never triggers
  it. Lifted back to `●` by the next successful probe or recall.

## [1.3.2]

### Fixed
- **Prompts no longer stall 10–30s replaying the warmup buffer** (#298). The
  ready marker (30s TTL) is only refreshed on the prompt path, so during any
  long agent turn it expired while the server was healthy; every tool trace
  then piled into the warmup buffer, and the next prompt's lookup hook —
  reading TTL expiry as "the server just came up" — synchronously replayed the
  whole backlog (N sequential `/remember/entry` calls, ~1s each, no deadline)
  before recall even started. Three moves, per the excellent analysis in #298:
  - **The buffer stops filling against a healthy server.** The per-tool-call
    store hook now uses `server_usable()`: on a stale marker it makes one
    bounded 1s `/health` probe and re-marks ready on success, keeping the
    marker fresh for the whole turn. It buffers only when the server is
    actually unreachable, and a failed probe is memoized for 10s so a real
    outage costs one probe per window, not one per tool call.
  - **The drain is out of the lookup hook.** `session-context-lookup.py` never
    replays the buffer anymore; `store-user-prompt.py` — the sibling hook on
    the same UserPromptSubmit event, which recall does not wait on — drains
    instead, after its fast bookkeeping. (Codex does not support async command
    hooks yet, so the drain still occupies that hook's window — but Codex runs
    matching hooks concurrently, and the new budget caps the worst case at
    ~20s versus unbounded before.) Entries buffered at the tail of a turn may
    land one turn later; that is the accepted trade for never stalling recall.
  - **The drain itself is time-boxed.** An overall budget
    (`COGNEE_DRAIN_BUDGET`, 20s; the detached SessionEnd/idle sync uses
    `COGNEE_DRAIN_BUDGET_FINAL`, 120s) stops the replay with the unreplayed
    tail preserved, and each entry's socket timeout is clamped to the budget
    remaining so one hung call cannot eat it all.
- **A poisoned session backlog can no longer be retried forever** (#298). One
  real incident had a SessionEnd worker grinding against a server that 503'd
  every write for 6.5 hours. Consecutive HTTP-status drain failures now back
  the session off exponentially (60s doubling, capped at 1h, logged as
  `warmup_drain_backoff`), skipped before any network I/O; progress resets the
  streak. Network errors deliberately don't count — the readiness gates
  already cover a down server, and backing off on them would delay recovery
  after a restart.

## [1.3.1]

### Fixed
- **One graph search per prompt, not two.** The per-prompt recall hook queried
  both `graph_context` and `graph` as separate scopes — a design from when
  `graph_context` was a cheap lookup of the distilled `improve()` snapshot. On
  cognee ≥ 1.4 the server aliases `graph_context` to `graph` (with a deprecation
  warning), so the pair silently ran the *same* full graph retrieval twice per
  prompt: once auto-routed to GRAPH_COMPLETION, once as the explicit
  HYBRID_COMPLETION. The two scopes are now folded into a single `graph` scope
  using HYBRID_COMPLETION, halving the most expensive part of every recall and
  removing the server-side deprecation warning. Results from the `graph` scope
  are still bucketed under the historical `graph_context` label, so the status
  line counters, `last_recall.json`, and the `g` count render unchanged.
- **Recall scopes run cheap-first, so agent guidance is never starved.** The
  scope order was session → trace → graph_context → graph → session_context;
  with both graph searches routinely consuming their full 2.5s timeout, the
  4-second recall budget was exhausted before `session_context` — standing agent
  guidance, ~50ms when it runs — ever fired, and it was silently skipped on
  every degraded prompt. The order is now session → trace → session_context →
  graph, so the only call that can eat a whole timeout runs last, against
  whatever budget remains.
- **Per-scope timeouts are clamped to the remaining recall budget.** The budget
  deadline was only checked *between* scopes, so a scope dispatched just before
  it could run a full `COGNEE_RECALL_TIMEOUT` past it — two slow scopes pushed a
  "4-second" recall to 5–7s of keystroke-to-answer latency, mathematically
  guaranteeing a `recall_budget_exceeded` event. Each call now gets
  `min(recall_timeout, budget remaining)`, and a scope with less than 0.2s of
  budget left is skipped outright instead of being dispatched with a doomed
  deadline. The budget is a hard ceiling now; `recall_budget_exceeded` only
  fires when the budget was genuinely spent on useful work.
- **No more "clamping SessionEnd hook timeout to 3s" warning at session start**
  (#303). The SessionEnd entry in `hooks.json` still declared `timeout: 120`
  from the era when the hook ran the final session sync inline; Codex hard-caps
  SessionEnd hooks at 3s (they block CLI exit) and warned about the clamp on
  every startup. The hook has long since done its real work in a detached
  worker not subject to hook timeouts — it only stops the idle watcher and
  spawns that worker, well inside 3s — so the vestigial declaration is removed
  and Codex's default applies. Sync behavior is unchanged.

## [1.3.0]

### Added
- **One-time configuration via `~/.cognee/.env`.** API keys and URLs
  (`COGNEE_BASE_URL`, `COGNEE_API_KEY`, `LLM_API_KEY`, and every other env var the
  plugin reads) no longer need to be exported in each shell: values placed in
  `~/.cognee/.env` — the durable cognee home shared with the Claude Code plugin —
  are injected into the environment at process start with setdefault semantics, so
  a real shell export still wins per terminal, and spawned processes (local
  server, watchers) inherit them unchanged. The file accepts pasted
  `export KEY=value` lines, is created with a commented template (mode `0600`) on
  first session start, and its location can be overridden with `COGNEE_ENV_FILE`.
  `doctor.py` gained an **Env File** row showing which keys the file defines
  (names only, never values) and which are overridden by shell exports. The
  parser tolerates Windows-written files: a UTF-8 BOM (PowerShell 5.1) is
  stripped, UTF-16 (a PS5 `>` redirect) is decoded, and CRLF line endings are
  handled — so copy/paste setup blocks work from any shell.

## [1.2.2]

### Fixed
- **Per-prompt recall now names its dataset.** `/api/v1/recall` was the only
  data-plane call that omitted one, so the server resolved *every* dataset the user
  can read and searched all of them on every prompt — on a machine with several
  datasets that is the graph scope paying for unrelated stores, one of which can be
  orders of magnitude larger than the plugin's own. It now sends
  `datasets: [<dataset>]`, matching the explicit-search path, which has always scoped
  this way. Applies to the per-prompt lookup and the pre-compact recall. The dataset
  is the usual one — `COGNEE_PLUGIN_DATASET` if set, otherwise `agent_sessions` — and
  the key is omitted entirely when no dataset is known.
- **One improve per session, instead of up to three at once.** The idle watcher,
  the store hook and the SessionEnd sync all bridge sessions, and the cross-hook
  `sync_lock` is bypassed in API mode — so in HTTP/cloud mode nothing stopped two of
  them submitting the *same* session concurrently. The server's own per-session lock
  answered the loser with a busy response, which drove a 15-second retry loop for up
  to ten minutes; in one real log that accounted for two thirds of all sessions and
  7,144 retry events. Improves are now claimed per session locally before submitting
  (`improve_session_lock`), so the loser skips immediately instead of waiting on work
  the winner is already doing. Freshness is unaffected: a skip reports not-synced, so
  the caller re-drives the whole drain+improve, and the server-side busy retry still
  covers a later attempt landing on a still-running pipeline. The claim covers both
  the HTTP and local-SDK paths, is released on exceptions, reclaims a dead holder's
  lock, and fails open — lock bookkeeping must never be why a session goes unsynced.

## [1.2.1]

### Fixed
- **The "update available" nudge now clears as soon as the update is applied.** The
  marker is a snapshot from the background check, and nothing rewrote it when the
  plugin actually updated — so the status kept advertising an update that was already
  installed until the next check. Both surfaces now compare the marker's
  `installed_version` against the version *running* and suppress the nudge on a
  mismatch. The comparison is against the running version, not the newest copy on
  disk, so a background auto-update that a session has not reloaded yet correctly
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
