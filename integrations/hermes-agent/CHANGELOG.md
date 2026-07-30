# Changelog

All notable changes to the Cognee memory plugin for Hermes Agent are documented
here.

The version must match the `version` field in both `pyproject.toml` and
`plugin.yaml`, and the `current_version` for `hermes-agent` in
[`integrations/inventory.yml`](../inventory.yml).

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0]

Moves the provider onto cognee's REST API, the way the Claude Code, Codex and
OpenClaw plugins already talk to it. The local cognee server was always there —
what changed is the client: routing through the SDK's `cognee.serve()` /
`CloudClient` silently dropped fields the server accepts.

### Changed

- **Breaking — the transport is now direct HTTP.** Requests are built and sent by
  the plugin (stdlib `urllib` only) instead of going through `cognee.serve()`.
  `COGNEE_TRANSPORT=sdk` restores the old client; `COGNEE_EMBEDDED=true` still uses
  the in-process SDK, which is the only transport that can run without a server.
- **Breaking — the local server moved from port 8000 to 8011.** This matches the
  other cognee agent plugins and leaves cognee's own default of 8000 to servers you
  run yourself, so the plugin no longer attaches to one by accident. Memory is
  unaffected — where it lives depends on the server's data directory, never the
  port. Set `COGNEE_LOCAL_PORT=8000` to keep the old behaviour, and stop any old
  plugin-started server on 8000 so two servers do not share one data directory.
- **Breaking — Hermes joins the shared brain.** The default dataset is now
  `agent_sessions` and local storage defaults to `~/.cognee/{data,system}` — the
  exact names and roots the Claude Code, Codex and OpenClaw plugins pin — so
  memory is shared across all cognee agent plugins on the machine, no matter
  which of them booted the server. `COGNEE_PLUGIN_DATASET` (the name the other
  plugins read) is the canonical dataset variable; `COGNEE_DATASET` stays as a
  lower-precedence alias. To keep a Hermes profile apart, set its own dataset, or
  its own `COGNEE_DATA_ROOT` / `COGNEE_SYSTEM_ROOT` *plus* `COGNEE_LOCAL_PORT`
  for full isolation. Old 0.1.x memory (dataset `hermes`, cognee's default
  roots) is not deleted but is no longer found by default —
  `COGNEE_DATASET=hermes` restores it. Previously roots were only set in embedded
  mode, so the spawned server fell back to cognee's global default location.
- **The minted API key is shared too.** It now lives at
  `~/.cognee-plugin/api_key.json` in the other plugins' exact format (previously
  `$HERMES_HOME/cognee-api-key.json`): whichever plugin mints first, the rest
  reuse. The spawned server's environment also mirrors the other plugins'
  bootstraps (`CACHE_ROOT_DIRECTORY`, `LLM_INSTRUCTOR_MODE=json_schema_mode`,
  `COGNEE_IMPROVE_SUBMIT_TIMEOUT=420`), and its log moved to
  `~/.cognee-plugin/hermes/server.log`, so the server behaves identically no
  matter which plugin starts it.

### Fixed

- **Session memory now reaches the permanent graph.** `improve()` is called with
  `session_ids`, which `CloudClient.improve` never forwarded — the session-to-graph
  bridge had silently become a dataset-wide improve.
- **An unreachable `COGNEE_BASE_URL` now fails at startup.** `cognee.serve()` logged
  a warning and returned a client regardless, so a bad URL only surfaced on the
  first real call.
- **The spawned server no longer leaks.** The plugin registers an agent connection
  on connect and unregisters on shutdown, so cognee's idle watchdog can stop a
  server nobody is using.
- **Local servers are authenticated.** An owner API key is minted once and cached
  at `~/.cognee-plugin/api_key.json`, instead of relying on the server having
  auth disabled.
- **A failed initialization now fails closed.** Hermes logs a provider's
  initialization error and starts anyway, so every entry point now refuses to run
  rather than operating an unconnected transport — which previously meant quietly
  falling back to in-process cognee and its single-writer databases.
- **No more cross-conversation context leak on `/reset`.** A recall still in flight
  for the previous conversation is discarded instead of landing in the fresh one.
- **Write gating is uniform.** `on_memory_write` now honours `agent_context`, so a
  subagent's built-in memory write is suppressed like its conversation turns.
- **`hermes backup` sees cognee storage.** `backup_paths()` reports roots pointed
  outside `HERMES_HOME`.
- **`COGNEE_AUTO_ROUTE=false` is honoured over HTTP**, translated to an explicit
  `GRAPH_COMPLETION` search type since the endpoint has no `auto_route` field.
- **Session turns actually reach the session cache.** They are stored as a typed
  QA entry via `/api/v1/remember/entry`, the endpoint the Claude Code plugin uses
  for the same purpose. Sending them as a document to `/api/v1/remember` with a
  `session_id` looked like it worked — the API answered
  `status: "session_stored"` — but the payload arrives as a multipart file, which
  cognee coerces to a `[UploadFile]` placeholder and deliberately *skips* for the
  session cache. Every turn was silently discarded and `improve()` had nothing to
  promote into the graph. Found by the live round-trip test; no mock could catch it,
  because the server reported success.
- The spawned server is also started with `CACHING=true` and `AUTO_FEEDBACK=true`,
  matching the Claude Code and OpenClaw plugins. Both are already cognee's defaults,
  so this is insurance against a future default change silently disabling the session
  tier, not a fix in itself.
- **A cold first boot no longer times out.** `COGNEE_SERVER_BOOT_TIMEOUT` now
  defaults to 600s (was 30s), matching the other plugins' boot deadline — a first
  boot runs DB migrations and can take minutes, and giving up early left memory
  off for the whole session. The long deadline is safe because the bootstrap now
  **fails fast when the spawned server dies**: a crashed child with nothing
  listening on the port raises immediately (pointing at the server log) instead
  of stalling the session start, while a child that merely lost the port-bind
  race to a concurrent starter still waits for the winner to become healthy.
- **cognee is now bounded to `>=1.2.1,<=1.4.0`** (was the loose
  `>=1.0.0,<2.0.0`). The wire contract this plugin depends on —
  `/api/v1/remember/entry` and `improve(session_ids)` — first shipped in 1.2.1,
  and a 1.0.x server would accept some of these requests and silently do the
  wrong thing. The 1.4.0 cap is the newest version verified against the full
  test suite and a live server boot (health, key minting, agent
  register/unregister); the lockfile resolves to 1.4.0.

### Added

- `COGNEE_TRANSPORT` — `http` (default) or `sdk`.
- A test suite grown from 34 to 270, including wire-format tests per transport and
  an opt-in live round trip against a real cognee server
  (`COGNEE_RUN_INTEGRATION=1`).

### Known limitations

- A *permanent* write cannot be linked to its originating session over HTTP:
  `/api/v1/remember` has no `session_ids` field. The session-to-graph bridge is
  unaffected. Logged once per session rather than dropped silently.
- `pip install cognee-integration-hermes-agent` cannot activate the provider:
  Hermes discovers memory providers by directory scan only. Install as a directory
  plugin under `$HERMES_HOME/plugins/cognee`.

## [0.1.0]

Initial standalone plugin: session-aware graph memory with recall, remember,
forget, and a session-end improve, over the cognee SDK.
