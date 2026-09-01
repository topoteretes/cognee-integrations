# Changelog

All notable changes to the **cognee-memory** Claude Code plugin are documented here.

The version here must match the `version` field in both `.claude-plugin/plugin.json`
and the plugin's entry in the repository-root `.claude-plugin/marketplace.json` — Claude
Code only offers an update when that string changes. Tag releases as
`cognee-memory-vX.Y.Z` (matching the repo's per-plugin tag convention).

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [1.5.0]

### Added
- **Plugin identity: the plugin can now run as its own cognee agent sub-user.**
  Cognee servers that expose `POST /api/v1/integrations/plugins/claude-code/provision`
  mint a dedicated agent identity (sub-user + labeled API key) per plugin, so the
  dashboard attributes sessions, traces, and datasets to *this plugin* instead of
  the shared principal key. The provisioned key is cached per service URL at
  `~/.cognee-plugin/claude-code/agent_key.json` and outranks the env/cached
  principal for data-plane traffic; datasets the agent creates are auto-shared
  to the parent user.
  - **Fresh installs provision automatically.** Existing installs deliberately
    stay on the principal key — their datasets are owned by it, and the
    parent→agent share is one-directional — unless opted in with
    `"plugin_identity": true` in config.json or `COGNEE_PLUGIN_IDENTITY=true`.
  - **Rotation-aware:** the server rotates (and revokes) the key on every
    provision call, so a cached key is never re-provisioned; a key revoked
    out-of-band (dashboard disconnect) is detected via the auth-rejected
    registration, dropped, and re-provisioned once. Servers without the
    endpoint (404) fall back to the principal silently.
  - `cognee-doctor` reports the new key source as **Plugin identity**.

### Changed
- **Agent connections now self-declare `type: "claude_code"`** at
  `POST /api/v1/agents/register` (previously the generic `"api"`), matching the
  server's connection-type registry so the integrations page recognizes the
  plugin without session-id-prefix heuristics.

## [1.4.3]

### Fixed
- **Local mode: configured backend providers now get their drivers installed
  (#232).** The self-managed install ran a bare `cognee==<pin>`, which carries
  no Postgres/Neo4j/Ollama/fastembed drivers, so pointing the plugin at any
  non-default backend (`DB_PROVIDER=postgres`, `VECTOR_DB_PROVIDER=pgvector`,
  `GRAPH_DATABASE_PROVIDER=neo4j`, `EMBEDDING_PROVIDER=fastembed`,
  `LLM_PROVIDER=ollama`) crashed the spawned server on its first use of that
  backend. The install now detects the configured providers from the same env
  vars cognee's own config classes read — exports and `~/.cognee/.env` alike —
  and installs exactly the matching cognee extras. Continues @GrowDev1's work
  in #271.
- **A backend configured after the venv was built no longer stays broken.**
  The at-pin install skip checked only the cognee version, so the drivers for
  a provider configured later never arrived — the pin was satisfied and the
  install never re-ran. The skip now also probes the venv for the required
  drivers and falls through to the install only when one is missing; a bare
  install done by another plugin sharing the venv can no longer strand a
  configured backend either.

### Changed
- The `~/.cognee/.env` template now lists the backend provider variables, so a
  local Postgres/Neo4j/Ollama/fastembed setup is discoverable in the same file
  that already holds the LLM key.

## [1.4.2]

### Fixed
- **Recall no longer reports an error for a graph that simply doesn't exist yet.**
  A dataset nobody has cognified yet has no graph, and the server answers the
  graph recall scope with 404 until the first sync lands — on a fresh install
  that is every prompt of the first session. That expected answer was logged as
  `recall_error` and counted against connection health alongside real failures.
  It is now logged as `recall_graph_not_built` (debug, not error) and kept out
  of the health accounting, so genuine failures stay visible.
- **The pending-prompt buffer no longer leaves an empty file behind per session.**
  Consuming the last buffered prompt wrote `{}` back instead of removing the
  file, so `pending/` collected one 2-byte husk per session forever. The buffer
  file is now deleted when its last entry is consumed, and the session-start
  sweep clears the husks older versions already left.
- **Cloud: creating a dataset through the API no longer fails on a redirect.**
  Cloud tenants answer `POST /api/v1/datasets` with a 307 to the trailing-slash
  path, and the stdlib HTTP client will not replay a POST body across a 307, so
  ensuring the target dataset (setup, and switching datasets mid-session) failed
  against cloud with a redirect status. The client now posts to
  `/api/v1/datasets/` directly.
- **Status line: a stale red ✕ now heals itself while you are idle.** Recovery
  from a recorded failure verdict (`unreachable`, `server_error`,
  `not_responding`, `auth_failed`) was prompt-driven: nothing re-checked the
  shared marker until a hook ran, so a server that came back while the terminal
  sat idle kept a red glyph on the bar for up to the 30-minute fade — long
  enough to tempt a needless restart. The session-long exit watcher now
  re-probes about once a minute (`COGNEE_CONN_REPROBE_INTERVAL`) while, and
  only while, the marker holds a failure state for this session's own
  `base_url`, and writes `ready` on success. Only a positive verdict is ever
  written — timeouts stay no-verdict, so this can clear a wrong red but never
  paint one — and `auth_failed` clears only on an authenticated success, since
  `/health` answering 200 says nothing about the key.

## [1.4.1]

### Changed
- **Status line: memory hits in plain words, plus a per-session total.** The
  `recall 4s/5t/0g/1a · saved 2p/41t/2a` strip is replaced by
  `5 memory hits · 12/40 turns had hits this session` — how many memories this
  turn's lookup injected — with `(3 from past sessions)` for the graph passages
  that came from an earlier session or a remembered document, i.e. what this
  conversation alone could not have supplied — then (faint) on how many of this
  session's prompts memory fired at all. A session with no hit yet reads `memory warming up
  (7 turns)` rather than `0/7`. The running total is accumulated by
  `session-context-lookup.py` in the per-session marker
  (`recall/<session>.json`, `session_totals`), so it costs no extra call, resets
  on `/clear` and continues across `--resume`. `COGNEE_STATUSLINE_COUNTS=full`
  restores the per-scope diagnostic strip; `false` still hides the segment.

## [1.4.0]

### Added
- **Switch datasets mid-session: `/cognee-memory:cognee-switch-datasets`.** Lists the
  datasets you can write to (owned by the principal behind your API key — `GET
  /api/v1/datasets` returns everything readable, so read-only ones are filtered out
  and counted), lets you pick one, and moves the launch: the current session is
  synced into its dataset first (`sync-session-to-graph.py --strict`; the switch
  aborts if that fails, `--force` to proceed anyway), a **new** Cognee session is
  registered on the target dataset under a fresh connection handle, and only then
  is the old handle released — so a local agent-mode server never drops to zero
  connections. Backed by `scripts/switch-dataset.py` (`--list [--json]`, `<name>
  [--force] [--json]`, `--session-key`).

- **`cognee-forget` skill — user-directed deletion of memory.** "Forget what we
  talked about tennis" now has a first-class flow: the agent syncs the live
  session (so unsynced content becomes a deletable document), lists the plugin
  dataset, judges candidate documents by their raw content, confirms with the
  user, and deletes each match via `POST /api/v1/forget`. Documents from the
  same session are treated as a group — deleting one while keeping its siblings
  would leave the topic recallable.
- **`scripts/cognee-forget.sh`** — the skill's server access. Subcommands
  (`sync`, `datasets`, `data`, `raw`, `forget`, `env`) each resolve credentials
  per invocation the same way the other wrappers do (shell env →
  `~/.cognee/.env` → the auto-minted local `api_key.json`), because exported
  variables don't survive across the agent's separate Bash calls. Every API
  command appends a final `HTTP <status>` line; with no key resolvable the
  helper exits 2 with guidance instead of sending a request that can only 401.
  The helper deliberately supports single-document deletion only — dataset-wide
  and `everything` scopes stay behind an explicit-user-request warning in the
  skill. Ids are validated as UUIDs and the request body is built with `json.dumps` rather than string interpolation, so a crafted id cannot redirect the request to another endpoint or append body fields — an injected `everything: true` would have deleted every dataset the user owns.
- **Mock-server forget surface + e2e tests.** `MockCogneeServer` now serves
  `GET /api/v1/datasets`, `GET /datasets/<id>/data`, `GET .../raw`, and
  `POST /api/v1/forget`; `tests/e2e/test_forget_script.py` runs the wrapper as
  a subprocess against it (credential resolution incl. the `api_key.json`
  fallback and the exit-2 path, payload shape, status trailer, 404 pass-through).

- **Code graph.** Repositories can be indexed into cognee's deterministic,
  enola-backed code graph (symbols, calls, imports, endpoints, dependencies)
  and queried from the plugin — with **no LLM or embedding calls** on either
  side. Requires a cognee server >= 1.5.3.
  - **Automatic indexing**: opening a session inside a git repository indexes
    it in the background at session start (never blocking the first prompt)
    and refreshes an already-indexed repo whose tree changed. New repos are
    auto-indexed only against a *local* server, where the code stays on the
    machine; a remote server needs `COGNEE_CODE_AUTOINDEX=always` or an explicit
    index. Non-git directories, repos with no source files, and repos over 3000
    source files are skipped (explicit indexing has no cap).
  - **Freshness**: the Stop hook re-submits an indexed repo when a turn changed
    its working tree, detected by a git fingerprint (HEAD, dirty set, tracked
    diff, untracked stats). Failures keep the fingerprint — the edits stay
    pending — behind an escalating backoff (30s → 15min cap) so an unresolved
    failure cannot re-submit once per turn forever; a new session always gets
    one attempt.
  - **Auto-recall code lane**: prompts naming an identifier-shaped token
    (`process_payment`, `UserService`, `billing/api.py`) inside an indexed repo
    get code facts injected under `=== Code graph facts ===`. The lane is
    additive to the semantic scopes, gated syntactically, and contributes
    nothing when it misses — conversational prompts are unchanged.
  - **Explicit tools**: `cognee-index-repo.sh <path-or-git-url>`,
    `cognee-search.sh "<seed>" --code [--code-query '<json>']` (operations:
    `query_facts`, `explore`, `traverse`, `find_path`, `impact_analysis`,
    `delta`), `cognee-remember.sh --file <path>` (uploads under the real
    filename so code routes as code, not prose), and a new `cognee-code` skill.
- One dataset per indexed repository, `codebase-<repo>-<digest>`. The path
  digest is load-bearing: same-basename checkouts sharing a dataset would share
  a graph database, where cognee's repo-scoped stale-node sweep would let each
  re-index delete the other's nodes. `--code` searches resolve the dataset from
  the current checkout.

### Fixed
- **cognee-search SKILL.md no longer claims searches span all authorized
  datasets.** The wrapper has always scoped recall to the plugin dataset
  (`COGNEE_PLUGIN_DATASET`, default `agent_sessions`); the doc now says so, and
  the raw `curl` examples pass `datasets` explicitly so following them doesn't
  search every readable dataset.

### Changed
- **Pinned cognee bumped to 1.5.3** (`_PINNED_COGNEE_VERSION` in
  `session-start.py`). 1.5.3 carries the session-invalidation work the forget
  skill depends on: deleting a document now also removes
  the session Q&A turns whose answers cited the deleted graph elements, the
  feedback and distilled guidance descending from them, and clamps the persist
  watermark to the surviving entry count so post-delete turns are not silently
  skipped by the next sync. Dataset-level deletes drop every session attributed
  to the dataset. The plugin always installs the exact pin so the server's
  lifespan migrations run on a known-good release.

  Documented core limit, reflected in the skill: agent **trace** entries carry
  no graph-element ids and are not matched, so trace content is not invalidated
  by a document delete and a later sync can re-persist it as new trace
  documents. The skill states this rather than promising the session cache is
  clean.

- **The active dataset now lives in the launch record**
  (`~/.cognee-plugin/claude-code/sessions/<host id>.json`), seeded at SessionStart
  from `COGNEE_PLUGIN_DATASET`/default. `config.get_dataset`, `load_resolved`, the
  `cognee-search.sh`/`cognee-remember.sh` wrappers, the idle and exit watchers and
  the status line all read it, so a switch is followed everywhere and survives
  `--resume`. A switched record beats an exported `COGNEE_SESSION_ID`.
- **Status line** shows the launch's recorded dataset and a faint `· switched` tag
  once it differs from the launch-time one.
- **Final sync covers every session the launch touched.** The record keeps a
  `touched` list of `{session_id, dataset, conn_uuid}` triples; the SessionEnd /
  exit-watcher sync bridges each pair (current last) and releases every handle, so a
  write that raced a switch is never lost.
- `sync-session-to-graph.py --strict` exits non-zero on an incomplete bridge.

- **Pinned cognee version is now `1.5.3`** (was `1.5.0`) — the release that
  opened `content_type="code"` on `/api/v1/remember` and the `code` recall
  scope. Installed into the managed venv on next session start.
- The freshness model is documented as a property of where the server runs:
  a local server reflects the working tree (uncommitted changes included); a
  cloud server reflects the last *pushed* commit, since its clone cannot see
  local edits.

## [1.3.3]

### Fixed
- **A replayed write can no longer duplicate a turn the server already holds.**
  The v1.3.2 failure buffering treated every retryable error the same, but a
  write that *times out* (or dies on a gateway 500/502/504) may still have
  committed server-side — and `/remember/entry` has no idempotency: every
  accepted write creates and embeds a fresh entry, so replaying a committed
  write stored the same content twice and fed the duplicate to the next
  improve. Failures are now classified at the buffering point
  (`write_outcome_ambiguous`): connection-refused/DNS failures and 503s
  provably never reached the cache and replay blind as before, while
  ambiguous outcomes are marked in the buffer and verified by the drain —
  one `GET /api/v1/sessions/{id}` per drain (only when an ambiguous entry is
  actually pending) supplies the server's recent QA/trace tails, and an entry
  whose content is already there is consumed without being re-sent (reported
  as `deduped` in the `warmup_drained` event). A failed verification read
  degrades to the old replay-everything behavior: a rare duplicate beats a
  lost turn. Also corrected two comments that claimed `/remember/entry`
  writes are "deduped server-side" — they never were; the single-drainer
  lock and this verify pass are the real guards.

### Changed
- **Pinned cognee version is now `1.5.0`** (was `1.4.2`). The plugin installs
  this into its own managed venv on session start, so existing installs pick it
  up on the next session; the local server's lifespan migrations handle any
  database upgrades on first start. Cognee 1.5.0 is a minor release with no
  user-facing breaking changes — it hardens large-scale dataset migrations,
  improves Ladybug graph-adapter reliability and performance, and adds
  `LLM_TEMPERATURE`/`LLM_SEED` plumbing. No plugin-side behavior changes.

## [1.3.2]

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

### Known issues
- **The PreCompact anchor is empty in server mode.** `pre-compact.py` now recalls
  over HTTP, but its seed recall deliberately sends an empty query (there is no
  user question at compaction time) and the server matches nothing on one, so no
  session or trace entries come back and no anchor is printed. Per-prompt recall
  is unaffected — this costs the summary carried across a compaction, not memory
  itself. The fix is server-side.

## [1.3.1]

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

## [1.3.0]

### Added
- **`COGNEE_BACKEND` per-terminal mode switch.** `~/.cognee/.env` may now hold
  the cloud vars (`COGNEE_BASE_URL`, `COGNEE_API_KEY`) *and* the local vars
  (`LLM_API_KEY`, …) together; with nothing exported, cloud wins as before.
  `export COGNEE_BACKEND=local` (or `=cloud`) flips a single terminal — the
  shared name switches both the Claude Code and Codex plugins at once, while
  `COGNEE_CLAUDE_BACKEND` targets this plugin only and beats the shared name.
- **Forced cloud is pinned, and misconfiguration is surfaced.** With
  `COGNEE_BACKEND=cloud` but no `COGNEE_BASE_URL`, the plugin no longer
  silently falls back to local (no local server boot, no venv build); the
  status line shows `✕ (missing_cognee_base_url)` and `cognee doctor`'s mode
  row explains what forced the decision and what is missing.

### Fixed
- **`COGNEE_CLAUDE_BACKEND=local` now holds on the HTTP hot paths.** The
  switch used to clear the cloud URL only in `load_config()`'s view, while
  recall/remember read `COGNEE_BASE_URL` from the environment — where the env
  file had already injected the cloud URL — so those calls still went to the
  cloud. A forced-local switch now scrubs `COGNEE_BASE_URL`/`COGNEE_API_KEY`
  from the process environment itself (with empty strings, so re-running the
  loader in child processes cannot re-inject the file's values).
- `COGNEE_CODEX_BACKEND` no longer flips this plugin: an export targeting the
  Codex plugin used to switch Claude Code's backend too.

## [1.2.6]

### Changed
- **Pinned cognee version is now `1.4.2`** (was `1.4.0`). The plugin installs
  this into its own managed venv on session start, so existing installs pick it
  up on the next session. No plugin-side behavior changes: cognee 1.4.1+ resolves
  an *omitted* session id to a per-dataset default (`default_session_<dataset_id>`),
  but the plugin always sends its explicit per-conversation session id, which
  passes through unchanged.

## [1.2.5]

### Added
- **Cloud credits in the status bar.** Cloud sessions now show the
  connected tenant's balance right after the mode — `credits: $14.23` in
  green, red once negative (a negative balance is real unfunded spend, the
  one state that must not hide) — followed by the approximate cost of the
  last memory operation, e.g. `· last turn ~$0.04`. Motivated by an incident
  where a tenant overshot its budget by ~$159 through the integration path
  with no client-side visibility at any point.
  - **Costs appear when the turn finishes, not one prompt later.** A dedicated
    hook (`credits-refresh.py`, async on `Stop` AND `StopFailure` — errored
    turns spend real money too) diffs the tenant's spend counter against the
    turn-start baseline and attributes the delta as `turn`; explicit
    `remember` and `improve` operations are attributed at their own
    completion points. Costs carry a `~` on purpose: the cloud aggregates
    spend asynchronously and concurrent operations overlap, so the delta is
    an attribution, not an invoice. Most conversational turns genuinely cost
    ~$0 — recall runs with `only_context=true` (no LLM completion) — so the
    label typically moves on improve/remember, while the balance refreshes
    every turn.
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

## [1.2.4]

### Fixed
- **The "update available" nudge now clears in the same session the update is
  applied in.** The v1.1.1 fix compared the marker against the *running* plugin
  version, but installs are version-pinned directories: after `/plugin update`
  the old copy keeps rendering the status line until a restart re-points it, so
  the running version never moved and the nudge survived the whole session.
  Both surfaces (status-line segment and SessionStart message) now also consult
  Claude Code's install registry (`~/.claude/plugins/installed_plugins.json`),
  which is rewritten the moment an update lands: once it records a version at or
  past the marker's `latest_version`, the nudge is suppressed — the status line
  clears on its next refresh (~2s), no restart needed. A missing or malformed
  registry changes nothing (previous behaviour). The Codex plugin is unaffected:
  it updates in place, so its existing running-version guard already clears
  mid-session.

## [1.2.3]

### Fixed
- **False `✕ (unreachable)` in the status line.** Probe and recall
  timeouts were classified as "unreachable" and persisted into the shared
  connection state, so a busy-but-healthy server randomly turned the bar red
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
    longer red a local bar, and vice versa), counts failures in a sliding
    window instead of forever, re-arms half-open after cooldown, never counts
    timeouts, and the status line renders its real trip reason.
  - The renderer shows a red ✕ only for fresh, definitive failures (30 min
    TTL); stale or ambiguous state renders no glyph.
  - Breaker state writes use tmp + atomic replace so readers never see a torn
    file.

### Added
- **`✕ (not_responding)` status** — distinct from `unreachable`: the server
  accepts connections but hasn't answered for N consecutive timeout-only
  prompts (default 3, `COGNEE_SLOW_STREAK_THRESHOLD`; streak window
  `COGNEE_SLOW_STREAK_WINDOW`, 600s). A single slow response never triggers
  it. Lifted back to `●` by the next successful probe or recall.

## [1.2.2]

### Fixed
- **Prompts no longer stall 10–30s replaying the warmup buffer** (#298). The
  ready marker (30s TTL) is only refreshed on the prompt path, so during any
  long agent turn it expired while the server was healthy; every tool trace
  then piled into the warmup buffer, and the next prompt's lookup hook —
  reading TTL expiry as "the server just came up" — synchronously replayed the
  whole backlog (N sequential `/remember/entry` calls, ~1s each, no deadline)
  before recall even started. Users paid a 10–30s frozen prompt after every
  long turn, often ending in a hook timeout that also discarded the recall
  context. Three moves, per the excellent analysis in #298:
  - **The buffer stops filling against a healthy server.** The per-tool-call
    store hook now uses `server_usable()`: on a stale marker it makes one
    bounded 1s `/health` probe and re-marks ready on success, keeping the
    marker fresh for the whole turn. It buffers only when the server is
    actually unreachable, and a failed probe is memoized for 10s so a real
    outage costs one probe per window, not one per tool call.
  - **The drain is off the keystroke path.** `session-context-lookup.py` never
    replays the buffer anymore; `store-user-prompt.py` — the async sibling on
    the same UserPromptSubmit event — drains instead, after its fast
    bookkeeping, so nothing user-visible waits on it. Entries buffered at the
    tail of a turn may land one turn later; that is the accepted trade for
    never stalling the prompt.
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

## [1.2.1]

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

## [1.1.2]

### Fixed
- **Per-prompt recall now names its dataset.** `/api/v1/recall` was the only
  data-plane call that omitted one, so the server resolved *every* dataset the user
  can read and searched all of them on every prompt — on a machine with several
  datasets that is the graph scope paying for unrelated stores, one of which can be
  orders of magnitude larger than the plugin's own. It now sends
  `datasets: [<dataset>]`, matching the explicit-search path
  (`cognee-search.sh`), which has always scoped this way. The dataset is the usual
  one — `COGNEE_PLUGIN_DATASET` if set, otherwise `agent_sessions` — and the key is
  omitted entirely when no dataset is known, so a standalone invocation behaves as
  before.
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
