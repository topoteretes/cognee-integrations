# Changelog

All notable changes to the Cognee Antigravity plugin are documented in this file.
The version must match `plugin.json` so Antigravity can identify the installed
package version.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [1.5.2]

### Fixed
- **The recall header no longer reports buffered writes as saved (SDK-467).** The
  store hook bumped the same save counter whether a trace or answer reached the
  server or was diverted to the warmup buffer, so an outage read as a normal run
  of saves — `saved last turn 1 prompt / 6 trace / 1 answer` on every prompt
  while nothing reached the server. Buffered writes now have their own counter
  kinds (`trace_buffered`, `answer_buffered`) and the header shows them as their
  own segment, followed by what still waits for replay across every session on
  the machine and how long the oldest entry has waited:

  ```
  Cognee memory: 0 memory hits · memory warming up (3 turns) · saved last turn 1 prompt / 0 trace / 0 answer · buffered last turn 6 trace / 1 answer (not saved yet) · 7 awaiting replay, oldest 20d
  ```

  Nothing is added when nothing was buffered and nothing awaits replay, so the
  healthy header reads exactly as before. To make the age visible, every buffered
  entry now carries a `_buffered_at` stamp in `~/.cognee-plugin/antigravity/bridge/`;
  like the ambiguous-replay marker it is stripped before the entry is sent, so
  nothing new reaches the server. Entries buffered before this release take their
  file's mtime, a lower bound on their age.
- **A prompt whose recall is skipped because the server is known bad now gets a
  header too.** It used to return nothing — no header, no sign that memory was
  off — and because the save counter was only read on a successful recall, the
  first header after recovery presented weeks of buffered writes as one turn's
  saves. The header now reads `Cognee memory: recall skipped (server unreachable)`
  (or `auth failed` / `server error` / `server not responding`), followed by the
  saved, buffered and awaiting-replay segments, and the counter is read and reset
  on every prompt. The model receives the same line as its context.
- **`cognee-plugin metrics` reports buffered writes apart from saves.** The
  offline rollup added warmup-buffered writes to the saved totals and ignored the
  after-error buffering entirely. It now has a `buffered` section (`trace` /
  `answer`) fed by `store_buffered_warming`, `trace_buffered_after_error` and
  `store_buffered_after_error`, and `saves` counts only what the server received.

## [1.5.1]

### Fixed
- **Hotfix: the hooks run on Python 3.9 again (SDK-617).** Every hook script
  failed at import time under a 3.9 `python3` — the one macOS ships with the
  Xcode Command Line Tools — with `TypeError: unsupported operand type(s) for |`,
  because `X | None` annotations are evaluated when a function is defined.
  Nothing in the hooks needs 3.10 at runtime: they are stdlib HTTP clients, and
  cognee runs in the uv-managed Python 3.12 venv the bootstrap builds. Every
  script now carries `from __future__ import annotations`, so the plugin works
  on any Python 3.9+ host; the shared suite runs on 3.9 in CI to keep it that
  way. Shipped in place, without a version bump.
- **The uv-less install fallback refuses a host python older than 3.10.** When
  uv is unavailable and cannot be downloaded, the bootstrap builds the runtime
  venv from the host interpreter, which then inherits cognee's 3.10+ floor.
  Instead of building a venv cognee cannot install into, it now logs
  `host_python_too_old_for_venv` to `~/.cognee-plugin/antigravity/hook.log`, prints the interpreter path and
  version to stderr, and leaves a marker that every following SessionStart turns
  into a systemMessage naming the fix (install uv or a 3.10+ python3) until a
  venv is built. README states the requirement up front: 3.9+ for the hooks,
  3.10+ only for that fallback.

### Changed
- **Per-prompt recall dispatches every scope at once.** The scopes (`session`,
  `trace`, `session_context`, `graph`, plus the `code` lane when it is armed)
  were requested one after another, so every cheap scope was a full round trip
  on top of the graph search — three of them against a cloud server — and an
  armed code lane could burn seconds before graph even started. All scopes are
  now in flight together and the prompt waits for the slowest one, not the sum;
  the results are folded into the same injected context, in the same order.
  With the graph search the only expensive call, a prompt's recall now costs
  about what the graph search alone costs.
  - The per-prompt recall now has one knob: `COGNEE_RECALL_BUDGET` (default
    4s) is the deadline every scope gets. With the scopes concurrent, a
    per-scope timeout and a whole-recall budget bounded the same interval, so
    `COGNEE_RECALL_TIMEOUT` is no longer read by this hook (it still bounds the
    explicit `cognee-search` path). `recall_budget_exceeded` fires only when
    the budget is too small for any request at all.
  - A refused connection or a 401/403 no longer cuts the fan-out short (every
    request is already in flight and fails in the same round trip); it is still
    recorded as one verdict per prompt, never one per scope.
  - `per_scope` in the `context_lookup_*` events keeps its canonical order and
    per-scope `elapsed_ms`, which now overlap rather than add up.
  - The `context_lookup_hit` / `context_lookup_empty` events now also carry the
    recall's aggregate `elapsed_ms` (previously Claude Code only): with the
    per-scope timings overlapping, the total is no longer their sum, so it is
    logged outright.

- **No background credits polling.** The exit watcher polled the billing
  overview every 5 minutes for the life of every open terminal so the
  status-line balance would not age out of its 15-minute TTL while idle. That
  was the plugin's only idle network traffic, and its throttle read a marker
  field that does not exist until a refresh has succeeded for the connected
  tenant — with no tenant binding (self-hosted remote server, unresolved
  connection lookup) it fell through to a refresh attempt every 2 seconds and
  ~14k `credits_refresh_skipped_no_tenant` log lines a day. The balance cannot
  move from this machine while it is idle, so the poll is gone: the hook-time
  refreshes (prompt start, turn end, remember, improve) are the whole cadence.
  The renderer no longer hides an old reading; past 15 minutes it appends an
  age hint (`credits: $14.23 (2h ago)`), and hides only once the entry is
  older than the marker's own 7-day prune. `COGNEE_CREDITS_CHECK_INTERVAL` and
  the `exit-watcher:credits_check_error` event are retired.

## [1.5.0]

### Added
- **Plugin identity: the plugin can now run as its own cognee agent sub-user.**
  Cognee servers that expose `POST /api/v1/integrations/plugins/antigravity/provision`
  mint a dedicated agent identity (sub-user + labeled API key) per plugin, so the
  dashboard attributes sessions, traces, and datasets to *this plugin* instead of
  the shared principal key. The provisioned key is cached per service URL at
  `~/.cognee-plugin/antigravity/agent_key.json` and outranks the env/cached
  principal for data-plane traffic; datasets the agent creates are auto-shared
  to the parent user.
  - **Identity policy is `COGNEE_PLUGIN_IDENTITY` = `auto` (default) / `true` /
    `false`.** `auto` provisions only in service of shared agent memory (below) and
    reverts to the principal when that cannot be wired, so nothing the principal
    owns is ever stranded; `true` is explicit and strict — provisioning is required
    and never falls back to the owner; `false` runs as the principal and ignores a
    cached identity.
  - **Safe create-only provisioning, credentials bound to server and principal.**
    Provisioning uses the SDK's `create_only` contract and never rotates an existing
    key; a credential the server rejected is blocked and never reused, and one bound
    to another principal is never used. Under `true` those stop with an error; under
    `auto` the plugin runs as the principal and logs why. Local startup is serialized
    with an OS lock; credential files are written atomically with owner-only
    permissions. Servers without `create_only` (or the provision endpoint) leave
    `auto` installs on the principal.
  - the doctor reports the new key source as **Plugin identity**.
- **Shared agent memory (default): one memory across all of your plugin
  agents.** A plugin identity is its own user, and cognee's grants flow
  child→parent only — left alone, per-plugin identities would silo memory
  (Antigravity could not recall what Claude Code stored). Session start now wires
  the agent into a shared `cognee-agent` role in your tenant (created for a
  tenant-less fresh install) with read+write on your datasets, backfilled on
  every launch and every ~60s by the idle watcher so a dataset another plugin
  creates shows up without a restart. The launch's dataset becomes a
  canonical, user-owned dataset addressed by UUID (`dataset_id`/`dataset_ids`
  on the launch record) — a name only resolves among datasets the caller owns,
  which would fork an empty per-agent copy — and recall, remember, the
  session-entry store, improve and the skills all address it that way;
  pre-existing same-named copies stay in the recall set.
  - **Opt out** with `"shared_agent_memory": false` in config.json or
    `COGNEE_SHARED_AGENT_MEMORY=false` for separated, per-plugin memory (the
    previous behaviour, name-addressed). The agent is removed from the
    shared role — it can no longer read or write your datasets — keeps its
    identity, and starts writing to its own, private dataset; what it shared
    before stays in your user's dataset (still yours, still visible in the
    dashboard). Re-enabling puts it back into the same role and dataset.
  - Degrades to separated memory — never fails a session — when the server
    has no permissions API or cannot store session entries by dataset UUID,
    when you are not the owner of your tenant, or when a tenant-less user
    already owns datasets (activating a tenant would hide them). Under
    `auto` an install that hits one of those stays on the principal.
  - Every tenant/role/grant call runs as the *principal*: an agent key can
    never widen its own access (server-enforced, owner-only).
  - the doctor shows **Memory Sharing** (`shared (role: cognee-agent)` /
    `separated (<reason>)` / `principal (...)`).
- **Dataset UUIDs throughout registration, remember, improve, recall, and
  switching.** A UUID-shaped dataset is addressed as an id; effective write
  permissions (not ownership alone) determine the datasets you can switch to,
  and a failed switch persistence keeps the previous session and unregisters
  the unused new connection.
- **Explicit graph read datasets** through `COGNEE_PLUGIN_READ_DATASET_IDS`
  (a JSON array of UUIDs): federated graph recall separate from the session's
  single write dataset; it takes precedence over the datasets shared memory
  resolved.
- **Agent connections now self-declare `type: "antigravity"`** at
  `POST /api/v1/agents/register` (previously the generic `"api"`). Note: the
  server's plugin registry does not list `antigravity` yet, so provisioning
  answers 404 and identity mode `auto` stays on the principal until it does.

## [1.4.3]

### Added

- Native Antigravity package metadata, four named hooks, and Cognee skills.
- A bounded transcript adapter that reads only the final 1 MiB of JSONL to map
  Antigravity invocations, tool output, and completed responses into Cognee memory
  events.
- Plugin-specific backend selection through `COGNEE_ANTIGRAVITY_BACKEND`, shared
  `~/.cognee/.env` configuration, and private hook state under
  `~/.cognee-plugin/antigravity/`.

### Changed

- Align the shared runtime with current Claude Code and Codex: provider extras,
  code-graph indexing, dataset-aware sync, bounded logs, stale-state cleanup,
  recall accounting, and persistent improve cooldowns.
- Remove legacy config-file routing and full-transcript sync fallbacks.
- Support documented `executionNum` Stop payloads, deduplicate retried tool
  steps, and correlate out-of-order tool results by their call identity.
- Renew bootstrap ownership when a conversation resumes after its host exits.

### Safety

- Installing with `agy plugin install` never edits Antigravity settings; the plugin
  is registered through its native manifest and hook declarations.
