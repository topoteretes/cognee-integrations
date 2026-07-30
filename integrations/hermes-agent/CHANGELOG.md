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
- **Breaking — local storage is now profile-scoped in local-server mode.**
  `data_root` / `system_root` default to `$HERMES_HOME/cognee/{data,system}`, as the
  configuration reference always claimed. Previously they were only set in embedded
  mode, so the spawned server fell back to cognee's global default and every Hermes
  profile shared one store. Existing repo installs will find their old memory at
  cognee's default location; point `COGNEE_DATA_ROOT` / `COGNEE_SYSTEM_ROOT` there
  to keep using it.

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
  under `HERMES_HOME`, instead of relying on the server having auth disabled.
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
