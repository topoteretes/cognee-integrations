# Changelog

All notable changes to the Cognee memory plugin for Hermes Agent are documented
here.

The version must match the `version` field in both `pyproject.toml` and
`plugin.yaml`, and the `current_version` for `hermes-agent` in
[`integrations/inventory.yml`](../inventory.yml).

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [1.2.0]

Parity release: brings the Hermes plugin up to the claude-code/codex
integrations (and the OpenClaw 2026.8.27 parity release) on every user-facing
memory operation, expressed the way Hermes consumes capabilities — as memory
provider tools plus `hermes cognee` CLI commands.

### Added

- **Recall session layers.** The per-prompt recall used a single
  `scope: auto` call, and with `datasets` + an explicit `search_type` in the
  request the server resolves that to graph-only — so cached Q&A turns,
  tool-call trace lessons and distilled agent guidance never reached the
  prompt. Recall now fans out one bounded call per scope, cheap lanes first
  (`session → trace → session_context → graph`, the graph lane on
  `HYBRID_COMPLETION` with `only_context`), each rendered as its own block
  (`<session_memory>`, `<trace_lessons>`, `<agent_guidance>`,
  `<graph_memory>`), under a shared budget (`COGNEE_RECALL_BUDGET`, 20s) so a
  slow graph search can never starve the cheap lanes. A failure in one lane
  never discards the others; a 404 on the graph lane (dataset not yet
  cognified — routine on a fresh install) is benign, not a breaker failure.
  The `cognee_recall` tool's `scope=session` now covers all three session
  layers. Opt out with `COGNEE_RECALL_LAYERS=false`.
- **Per-document forget.** `cognee_forget` is now two-phase and user-directed
  ("forget what we said about tennis"): `action=find` lists candidate
  documents with raw-text previews and matched terms; `action=forget` deletes
  only the listed `data_ids`, one `POST /forget` (`datasetId`/`dataId`) each,
  and only with `confirm=true`. Ids are validated as UUIDs before they touch a
  request; whole-dataset deletion requires an explicit
  `everything_in_dataset=true`, and the all-datasets wipe is not expressible
  from the tool by construction. Targeted session-cache invalidation on delete
  needs cognee >= 1.5.3.
- **`cognee_switch_dataset` tool** — move one conversation to another dataset:
  `list` / `current` / `switch` / `reset`. A switch flushes the pending turn,
  bridges the current session into its old dataset (`improve` on the server's
  background pipeline — strict: it aborts on failure unless `force=true`, in
  which case the un-bridged session is recorded and re-submitted at session
  end), ensures the target, then re-points capture, recall and the session-end
  improve (and the crash-safe exit watcher) under a fresh cognee session id
  (`hermes_<id>__N`). Overrides persist per conversation in
  `~/.cognee-plugin/hermes/dataset-overrides.json` and are re-applied when the
  same Hermes session initializes again. `COGNEE_DATASET_SWITCH_TOOL=false`
  removes the tool.
- **Code graph.** `hermes cognee index-repo <path|url> [--dataset]
  [--index-vectors] [--wait <s>]` indexes a repository into cognee's
  deterministic code graph (enola pipeline, no LLM/embedding calls, one
  `codebase-<repo>-<digest>` dataset per repo — the path digest keeps
  same-basename checkouts from sharing a graph); `cognee_code_search` answers
  structural questions exactly (`query_facts`, `explore`, `traverse`,
  `find_path`, `impact_analysis`, `delta`); an additive `code` recall lane
  fires only when a prompt names an identifier-shaped token AND the launch
  directory sits inside an indexed repo (or `COGNEE_CODE_DATASETS` names one).
  Autoindex and per-turn re-ingest are deliberately not ported — Hermes is
  rarely launched inside a checkout (the OpenClaw plugin made the same call).
  Indexed repos are recorded under `~/.cognee-plugin/hermes/code-graph/`.
  Requires cognee >= 1.5.3. `COGNEE_CODE_SEARCH_TOOL` / `COGNEE_CODE_GRAPH_RECALL`
  opt out.
- **Memory steer.** The system-prompt block now asserts Cognee as the
  preferred, authoritative long-term memory over Hermes' built-in memory — the
  counterpart of claude-code's `COGNEE_PREFER_MEMORY` and openclaw's
  `memorySteer`. `COGNEE_MEMORY_STEER=false` disables it,
  `COGNEE_MEMORY_STEER_TEXT` replaces the wording.
- **Memory-hit visibility.** The injected recall block opens with plain words
  on what memory just did — `4 memory hits this turn (2 beyond this session) ·
  3/7 turns had hits this session` — with per-session totals that reset when a
  conversation resets. `COGNEE_MEMORY_HITS=false` hides it.
- **Version display + update check.** `hermes cognee version`, and both it and
  `hermes cognee status` now carry an "update available" hint from a
  rate-limited, fail-silent PyPI check (cached in
  `~/.cognee-plugin/hermes/update-check.json`; `COGNEE_UPDATE_CHECK`,
  `COGNEE_UPDATE_CHECK_INTERVAL`, `--check-updates` forces a live check).
  CLI-only — the session path never checks. The nudge stays two-step on
  purpose: `pip install -U` then `cognee-hermes-install`, because Hermes runs
  the installed copy.

### Changed

- **Breaking — cognee is now pinned to exactly 1.5.3** (`cognee==1.5.3`; the
  repo's pin checker accepts exact pins as bounding both sides). The installed
  package doubles as the local server this plugin spawns, and this release's
  features set a hard floor there: `content_type="code"` repo indexing, the
  `code` recall scope, and targeted session invalidation on document delete
  all first shipped in 1.5.3, so a 1.4.x resolve would silently strip the code
  graph and let a later sync re-persist forgotten content. The claude-code,
  codex and openclaw plugins pin their server to exactly 1.5.3 for the same
  reason; a *remote* server (`COGNEE_BASE_URL`) is unaffected by the pin but
  needs >= 1.5.3 for the same features, and an older one now gets clear
  errors on the code paths instead of quiet degradation.
- The HTTP transport grew the endpoints the new features stand on:
  `GET /datasets`, `GET /datasets/{id}/data`, `GET /datasets/{id}/data/{id}/raw`,
  single-document `POST /forget`, code indexing via `POST /remember`
  (`content_type=code`), `GET /datasets/status`, and `recall` now carries
  `scope` lists, `context_profile`, `code_query` and `only_context`. The SDK
  transport reports these operations as HTTP-only instead of failing obscurely.

### Known limitations

- Turns from the live conversation become deletable documents only after the
  session is bridged to the graph; `cognee_forget` says so in its envelope.
- The dataset listing in `cognee_switch_dataset` cannot tell read-only
  datasets apart client-side; a switch to one fails at the ensure step with a
  clear error instead.

## [1.1.0]

Hardens the plugin against silently corrupted search indexes with local Ollama
embedding models (live-diagnosed on a meeting-notes ingestion: the pipeline
reported success on every document while all retrieval timed out). cognee sizes
its chunks from `EMBEDDING_MAX_COMPLETION_TOKENS`, whose 8191 default is far
above any local embedding model's context; every substantial text overflowed
Ollama, and cognee mean-pooled the pieces into lossy vectors while still
reporting success — the only trace an `Ollama embedding error` line in the
server log.

### Added

- **Safe embedding defaults at server spawn.** With `EMBEDDING_PROVIDER=ollama`,
  the spawned server (and embedded mode) now gets a context-matched
  `EMBEDDING_MAX_COMPLETION_TOKENS` for recognized models (`all-minilm`,
  `nomic-embed-text`, `mxbai-embed-large`, `bge-m3`, and others — a conservative
  512 for anything unrecognized) plus the matching `HUGGINGFACE_TOKENIZER`, so
  cognee chunks within the model's real limits. Explicit values always win; no
  effect on other providers.
- **Overflow surfacing.** The HTTP transport tails the spawned server's log
  between calls; a fresh embedding-context overflow now lands in the tool
  envelope — a `warning` on results/successful writes, an `error` on an empty
  recall — naming the env levers and the recovery runbook, instead of degrading
  silently.
- **Timeout advice.** A timed-out recall error now says why it is likely slow
  (GRAPH_COMPLETION runs an LLM per query) and what to do (`search_type=CHUNKS`,
  `scope=session`, or `COGNEE_RECALL_TIMEOUT`); the `search_type` tool schema
  explains the CHUNKS/GRAPH_COMPLETION trade-off so the model can self-route.
- **Docs.** `RUNBOOK.md` (pause ingestion → fix env → restart server → rebuild
  dataset → verify → resume), `.env.example`, a README section on Ollama
  embedding settings, and README rows for the previously undocumented
  `COGNEE_RECALL_TIMEOUT` / `COGNEE_WRITE_TIMEOUT` / `COGNEE_IMPROVE_TIMEOUT`.

## [1.0.0]

First stable release, published to PyPI. Moves the provider onto cognee's REST
API, the way the Claude Code, Codex and OpenClaw plugins already talk to it. The local cognee server was always there —
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
- **Recall can see the session cache.** Over HTTP, `search_type` is now always
  sent — as an explicit `null` when auto-routing — and the scope travels by name
  in cognee's own `scope` field. `/api/v1/recall` defaults a *missing*
  `search_type` to `GRAPH_COMPLETION`, and cognee folds the session cache into an
  `auto` scope only while the search type is null, so leaving the key out
  resolved every scope — `session`, `auto` and `graph` alike — to graph-only.
  Turns were being written to a cache that nothing could read until `improve()`
  promoted them at session end.
- **`COGNEE_AUTO_ROUTE` is honoured over HTTP.** Same root cause: with the key
  omitted, `true` and `false` produced byte-identical requests and the query
  classifier never ran.
- **The session-end improve is no longer killed by the server it runs on, and no
  longer delays your exit.** Closing a session is now handed to the detached
  worker that already covered crashes: it runs `improve()` to completion and only
  then unregisters, while Hermes exits immediately. Previously the improve was
  submitted in the background and `shutdown()` unregistered straight after, which
  drops the agent count to zero — and `COGNEE_AGENT_MODE`'s watchdog SIGTERMs the
  server within 60s of that, with no regard for running pipelines, so a graph
  build (routinely longer than 60s) was liable to be cut in half. Doing it
  synchronously in-process would have fixed the ordering by making the user wait,
  so it happens out of process instead, matching how the claude-code, codex and
  openclaw plugins close a session. Both paths claim a once-marker, so a session
  is closed exactly once. `COGNEE_IMPROVE_BACKGROUND` now opts out of the handoff
  and keeps the work in-process.
- **An unreachable `COGNEE_BASE_URL` now fails at startup.** `cognee.serve()` logged
  a warning and returned a client regardless, so a bad URL only surfaced on the
  first real call.
- **A remote `COGNEE_BASE_URL` without `COGNEE_API_KEY` also fails at startup.**
  Key minting is local-only — remote servers (Cognee Cloud included) expose no
  login route to mint from, and the claude-code/openclaw plugins require a key
  for remote targets for the same reason. Previously the plugin continued
  unauthenticated and every call 401'd one at a time; it also no longer sends
  the default-user login credentials to non-local hosts.
- **The spawned server no longer leaks.** The plugin registers an agent connection
  on connect and unregisters on shutdown, so cognee's idle watchdog can stop a
  server nobody is using.
- **The exit watcher is safe on Windows.** Its liveness probe no longer uses
  `os.kill(pid, 0)` there — CPython implements non-console "signals" on Windows
  as `TerminateProcess`, so probing would have killed the running Hermes — and
  queries the process handle (`OpenProcess`/`GetExitCodeProcess`) instead.
  Group/broadcast pids (`<= 0`) are also never treated as watchable.
- **A crashed Hermes no longer loses its session or strands the server.** In
  server/remote mode the plugin arms a small detached exit watcher (the pattern
  the claude-code/codex/openclaw plugins use) that polls the Hermes PID; on an
  unclean death it runs `improve(session_ids=[...])` synchronously — improve
  *before* unregister, so the idle watchdog cannot tear the server down
  mid-promotion — then unregisters the agent connection. A clean shutdown
  disarms it first, so nothing double-fires; a session end that already bridged
  stands down only the improve half. State and log live under
  `~/.cognee-plugin/hermes/`. Embedded mode gets no watcher — there is no server
  to outlive the process.
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

- **pip is now a working install channel.** The wheel ships the plugin-root
  files (`plugin.yaml`, the `cli.py` shim, `after-install.md`) as package data,
  and a new `cognee-hermes-install` console script copies the plugin into
  `$HERMES_HOME/plugins/cognee` — the only place Hermes' directory scan looks.
  `hermes cognee status` now shows the installed plugin version and warns when
  the pip package is newer than the installed copy (after `pip install -U`,
  re-run `cognee-hermes-install`). Releases publish to PyPI from CI on
  `hermes-agent-v*` tags.
- **The dataset is ensured at startup** (idempotent `POST /api/v1/datasets`, the
  other plugins' bootstrap call), so a session that opens with a recall on a
  fresh server or cloud tenant no longer hits a missing dataset. Best-effort:
  writes create the dataset implicitly anyway.
- `COGNEE_TRANSPORT` — `http` (default) or `sdk`.
- A test suite grown from 34 to 270, including wire-format tests per transport and
  an opt-in live round trip against a real cognee server
  (`COGNEE_RUN_INTEGRATION=1`).

### Known limitations

- A *permanent* write cannot be linked to its originating session over HTTP:
  `/api/v1/remember` has no `session_ids` field. The session-to-graph bridge is
  unaffected. Logged once per session rather than dropped silently.
- `pip install` alone cannot activate the provider — Hermes discovers memory
  providers by directory scan only — which is why `cognee-hermes-install`
  exists, and why it must be re-run after every `pip install -U`.

## [0.1.0]

Initial standalone plugin: session-aware graph memory with recall, remember,
forget, and a session-end improve, over the cognee SDK.
