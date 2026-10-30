# Changelog

All notable changes to the **@cognee/cognee-openclaw** OpenClaw memory plugin are documented here.

The version here must match the `version` field in `package.json` (and `package-lock.json`)
and the `PLUGIN_VERSION` fallback in `src/version.ts` — a drift-guard test
(`__tests__/unit/test_version.ts`) fails when they disagree, and `openclaw cognee status`
reports an update only when the published npm version changes. Tag releases as
`openclaw-vYYYY.M.D` (matching the repo's per-plugin tag convention).

The format is based on [Keep a Changelog](https://keepachangelog.com/). Versions are
date-based (`YYYY.M.D`), matching the OpenClaw plugin ecosystem.

## [2026.10.26]

### Added
- **`openclaw cognee keys`** lists the per-machine API keys of every cognee plugin agent of
  your user (Claude Code, Codex, …), most recently used first, and `--revoke <id>
  --plugin <key>` revokes one. Cognee's provision route now adds one key per machine and
  caps them (cognee#6113); a full agent refuses a new machine a key, and this is how a
  slot is freed from OpenClaw. `CogneeHttpClient` gains `listPluginStatuses`,
  `listPluginKeys` and `revokePluginKey`.

### Fixed
- **The boot script says why the runtime could not be built.** When `uv` cannot
  build the cognee venv and the stdlib `python3 -m venv` fallback fails too, the error
  marker (and `openclaw cognee status`) used to read only
  `Command '[..., '-m', 'venv', ...]' returned non-zero exit status 1`. It now keeps
  uv's own reason first and the venv module's stderr after it, the way the pip
  fallback already did, so a missing `ensurepip` or an interpreter the runner image
  no longer ships is named instead of guessed.

## [2026.10.19]

### Changed
- **Bundled cognee is 1.6.5** (was 1.6.3; `COGNEE_VERSION` in `src/server.ts`, and
  `cognee-docker-compose.yaml` runs `cognee/cognee:1.6.5`). The shared
  `~/.cognee-plugin/venv` is upgraded on the next cold boot, which runs that
  release's one new migration: additive columns that count embedding tokens apart
  from LLM tokens. The Claude Code, Codex and Antigravity plugins, which share the
  venv and database, move to 1.6.5 at the same time: a 1.6.3 server refuses to start
  on a database a 1.6.5 server already migrated. 1.6.5 lets typed session entries
  carry a `node_set` and refuses a typed entry that names a different dataset than
  the one its session is bound to.

## [2026.10.13]

Brings the plugin to cognee 1.6.3 and to parity with the Claude Code and Codex
plugins: graph-only recall trimmed to what the agent does not already have, the
capture policy with secret redaction, a write buffer for captures made while the
server is away, the recap tool, and a boot that never downloads an interpreter.

### Added
- **Capture policy and secret redaction**, the rules of the Claude Code and Codex
  `COGNEE_CAPTURE_*` settings. `captureTools` / `captureDenyTools` choose which tool
  calls are captured: a name, an fnmatch glob, or `Tool(prefix:*)` matched against
  the first word of a shell command (`exec(git:*)`; `cd x && git …` counts as `cd`),
  matched case-insensitively. Calls touching `.env*`, `*.pem`, `*.key`, ssh and cloud
  credentials, `secrets.*` or `credentials.*` (plus `captureDenyPaths`) are never
  captured. `captureRedact` (on by default) scrubs private keys, bearer and basic
  tokens, DB URLs, credential assignments, vendor keys, bcrypt hashes and
  secret-named fields from captured prompts, answers and tool calls, plus
  `captureRedactPatterns`. A pattern the plugin cannot parse fails closed: nothing is
  captured until it is fixed, and the gateway log says why. Each unset key falls
  back to its `COGNEE_CAPTURE_*` variable. Until now a trace stored tool arguments
  and output as they were; the only filter skipped commands mentioning `cognee`.
  Shared cases: `integrations/conformance/capture_policy_cases.json`.
- **Captures made while the server is away are replayed.** A trace or QA write that
  never reached the server (the connection could not be opened: refused while it
  boots, host unreachable, DNS failure; or a 503) is queued in
  `~/.openclaw/memory/cognee/pending-entries.json` (newest 500, kept a week) and
  replayed after the next successful write, on gateway start, and, for the ending
  session, before its session-end `/improve`. A write that timed out, was cut off
  mid-request, or failed otherwise is not replayed, since the server may already
  have stored it, and each replayed entry leaves the file as soon as it is stored.
  Until now such writes were logged and dropped, and every turn of a cold boot was
  lost.
- **`memory_recap` tool and `openclaw cognee standup|digest|timeline`**, a port of
  the Claude Code and Codex recap skills: what each agent worked on and left open,
  the lessons distilled into memory, or a dated timeline of one topic, built from
  the sessions Cognee recorded and framed as data for the model to summarize.
  Sessions are grouped by agent. A recap only covers sessions recorded into the
  calling agent's own datasets, so on a shared gateway one agent never sees another
  agent's or user's conversations (`allSessions` adds other clients' sessions in
  those datasets, never other datasets). The server cannot filter sessions by
  dataset, so a recap reads up to 2,500 of the newest sessions and says so when the
  server had more, or when `maxSessions` left some out, instead of passing a partial
  window off as complete. `recapTool: false` leaves the tool out.
- **`recallPassageChars` / `recallContextChars`** (defaults 2000 / 12000, or
  `COGNEE_RECALL_PASSAGE_CHARS` / `COGNEE_RECALL_CONTEXT_CHARS`) bound the injected
  recall; see Changed.
- **Slow-server probe.** After three prompts in a row whose recall only timed out,
  each prompt first spends a 3 s `/health` probe and skips recall while the server
  does not answer, instead of waiting out the whole budget. Any answered recall
  ends it.
- **A user dataset without a user id is called out.** With `userDatasetPrefix` set
  and no `userId` (or `OPENCLAW_USER_ID`), the user scope falls back to
  `<prefix>-default`, one dataset every user shares; gateway start now warns.
- **`--agent` on `openclaw cognee improve` and `visualise`**, and `--dataset` on
  `visualise`. Under multi-scope both used to act on the configured `agentId`'s
  dataset only, and `visualise` found no dataset at all.
- **"What this plugin runs, stores and sends"** section in the README.
- **`chunkSize` setting** ([#428](https://github.com/topoteretes/cognee-integrations/issues/428)).
  File sync never sent `chunk_size`, so the server chunked every memory file at its
  form default of 4096 tokens, above the input limit of embedding models such as
  `bge-m3` (1500). Set `chunkSize` to send it on `/remember`. It is opt-in: unset
  sends nothing, and the server default applies as before. Files updated in place
  still use the server default, because `/update` takes no chunk size.

### Changed
- **Bundled cognee is 1.6.3** (was 1.6.0; `COGNEE_VERSION` in `src/server.ts`, and
  `cognee-docker-compose.yaml` runs `cognee/cognee:1.6.3`). The shared
  `~/.cognee-plugin/venv` is upgraded on the next cold boot, which runs that
  release's migrations (1.6.3 adds a capability layer for permissions). 1.6.1 carries
  chunk metadata into hybrid retrieval and makes `dlt` a core dependency; 1.6.2 sizes
  chunks to the embedding model's input limit (an explicit `chunkSize` above it is
  lowered, with a server-side warning) and explains a bare 401; 1.6.3 routes
  date-scoped questions to temporal search when no search type is given (the plugin
  always sends one) and moves LanceDB to 0.39.
- **Recall injects the retrieved context only, not the conversation the agent
  already has (SDK-904).** The graph recall is now sent without the session id:
  with it, cognee >= 1.6.0 put this session's recent turns in front of the item's
  text, which was most of a mid-session payload. Each item is then cut down to its
  retrieved context: the question template goes, passages recorded in this session
  go, each passage is capped at `recallPassageChars` (cut at a paragraph break), the
  whole at `recallContextChars` (dropping whole passages from the end), and entity
  lines that say nothing are pruned. Text that does not parse (an older server's
  bare context) is injected whole. This session's guidance comes from its own
  `scope: ["session_context"]` request against the capture dataset and is injected
  as `<cognee_guidance>`. The same rules as the Claude Code and Codex plugins,
  shared through `integrations/conformance/recall_text_cases.json`. On top of that,
  `recallContextChars` bounds the whole injection (guidance first, then each
  dataset's memory, then code facts; the block that crosses it is cut at a line and
  marked), so several scopes cannot add up past it. The graph, guidance and code
  lanes are independent: guidance is injected even when the graph has nothing or
  fails, and a lane not done by the recall deadline is dropped instead of costing
  the turn the others. `maxTokens` and
  `recallSessionLayers` are deprecated no-ops, kept so existing configs validate.
- **Recall timeouts are 20 s** (`recallTimeoutMs` and `recallBudgetMs`, were 10 s and
  12 s), the budget the Claude Code and Codex plugins moved to.
- **The boot never downloads an interpreter.** `ensure_and_boot.py` used
  `uv venv --python 3.12`, which let uv download a standalone Python. It now asks uv
  for any 3.10–3.14 already on the machine with `UV_PYTHON_DOWNLOADS=never`, and
  falls back to `python3.14` … `python3.10`, `python3`, `python` with `-m venv` (also
  when uv finds none). The pinned `cognee` package is the only download. With no
  suitable interpreter it refuses and says so, as before. When uv's install fails,
  the pip fallback first seeds pip into the venv uv built (uv venvs have none), and
  the boot error recorded for the gateway log leads with uv's own reason.
- **The local server starts in `~/.cognee-plugin`, and reads its settings from
  `~/.cognee/.env`.** cognee >= 1.6.2 loads the first `.env` it finds walking up from
  its working directory and lets it override the process environment, so a `.env`
  in the directory the gateway was started from could replace the default user,
  data directories or auth settings the boot passes. The server now runs from the
  plugin's state directory instead, and the boot script loads `~/.cognee/.env` (or
  `COGNEE_ENV_FILE`) the way the Claude Code and Codex plugins do: values act like
  exports, a variable already in the environment wins, and names such as `PATH` or
  `LD_*` are skipped. **If you kept `LLM_API_KEY` (or other server settings) in a
  `.env` in the directory you start the gateway from, move it to `~/.cognee/.env`**,
  or export it; that file is no longer read, and without a key the server cannot
  build the graph.

### Fixed
- **Long-running ingestion requests can exceed Node's 300s bundled-undici header timeout**
  ([#429](https://github.com/topoteretes/cognee-integrations/issues/429)). Requests configured for
  300 seconds or longer now use a package-local undici dispatcher with header/body
  timeouts disabled, while the plugin's existing `AbortController` remains the configured
  request deadline. Multipart
  bodies are rebuilt with undici's own `FormData`, avoiding cross-version dispatcher/body
  incompatibilities. This covers both `/remember` and `/update`.
  The 401 re-login retry now keeps its abort deadline while it reads the response
  body; it used to clear the timer first, so a stalled body could hang the request.
  `undici` is pinned to exactly 7.30.0, the plugin's only runtime dependency.
  Contributed by @Dotbo-7210 (#470).
- **Documented settings no longer get the plugin quarantined**
  ([#438](https://github.com/topoteretes/cognee-integrations/issues/438)).
  `perAgentMemory`, `noiseTriggers` and `noisePatterns` are documented and read by
  the plugin, but were missing from `configSchema` in `openclaw.plugin.json`, which
  rejects unknown keys. Setting any of them failed OpenClaw's config validation,
  and `openclaw doctor` quarantined the whole plugin config, disabling the plugin.
  Per-agent memory could not be turned on at all. All three are now declared, and
  a test keeps the schema in step with the settings `config.ts` reads.
- **Dataset names are sanitized for cognee.** cognee rejects a dataset name
  containing a space or a dot. The configured `datasetName` and every scope's
  derived name (company, user, agent — an email user id carries dots) now have
  spaces and dots replaced with `_`; every name the server already accepts stays
  exactly as it is. The dataset switch no longer accepts `.` in a name, and
  code-graph datasets for repositories with a dot in their name are now dot-free.
  Shared rule: `integrations/conformance/dataset_name_cases.json`. First
  implemented by @eiza763 (#226).
- **The exit watcher's API key is no longer visible to other local users.** It
  was passed in the watcher's command-line arguments, which any local user can
  read with `ps` or `/proc/<pid>/cmdline` for as long as the watcher runs. It now
  travels in the watcher's environment (`COGNEE_API_KEY`), matching the earlier
  fix for the Claude Code and Codex plugins (#395). Contributed by @pinzo (#425).
- **Windows memory files in subdirectories sync again.** `memory\2026-10-02.md`
  was uploaded as `memory\2026-10-02-md` while cognee stored `memory_2026-10-02-md`,
  so the plugin never matched the returned id, and the next edit was re-added
  instead of updated (409). Both separators are now sanitized. Contributed by
  @susumanin (#466).
- **Session-end improve stops on an exhausted LLM budget.** cognee 1.6.3 answers 402
  when the server's LLM budget runs out; the session-end chain retried it twice more
  10 s apart. It now logs why and stops.
- **Blank `node_set` entries are never sent.** cognee 1.6.3 rejects them with 400.
- **The exit watcher bridges the right session after a dataset switch.** It was given
  the conversation's session and dataset when spawned, so a gateway that died after
  `memory_switch_dataset` bridged the old session into the old dataset and left the
  new one unbridged. The plugin now keeps the current pair next to the watcher's
  pidfile.
- **Recall finds a dataset created after the gateway started.** A lookup that found
  no dataset by name (a fresh install's first prompt, before anything was stored)
  was cached for the gateway's lifetime, so recall stayed off until a restart even
  after memory had been written. A miss is now retried after 30 s.
- **Sessions are recorded with their dataset from the first turn.** `/remember/entry`
  does not create the capture dataset, and the server records a session's dataset
  from the write that opens it, so the first session on a fresh install was stored
  without one. Session start now creates the capture dataset (create-or-return)
  before the first capture, which also lets recall and recaps find it at once.
- **A configured `apiKey` is honored at session end.** The session-end chain
  resolved the key without the configured one, so a cached minted key in
  `~/.cognee-plugin/api_key.json` could win over it.

## [2026.9.22]

### Changed
- **Bundled cognee is 1.6.0** (was 1.5.4; `COGNEE_VERSION` in `src/server.ts`, and
  `cognee-docker-compose.yaml` runs `cognee/cognee:1.6.0`). The shared
  `~/.cognee-plugin/venv` is upgraded on the next gateway start, which runs that
  release's migrations; 1.6.0 made `fastembed` and `onnxruntime` core dependencies,
  so the venv grows. The pin stays in step with the claude-code/codex/antigravity
  plugins that share the venv.
- **One recall request per prompt, injected as the LLM would have received it
  (SDK-741, cognee #5085).** With `only_context`, a completion search type on cognee
  1.6.0 returns one graph item per dataset whose `text` is the full LLM input: the
  conversation history for the session, the question with the retrieved context
  rendered through the retriever's template, and the session guidance block (a
  separate `system_prompt` field carries the task template and is ignored). The
  per-prompt recall therefore makes exactly one request — `scope: ["graph"]`, the
  configured `searchType` (default `HYBRID_COMPLETION`), `only_context: true`, the
  session id and every recall dataset id — plus the identifier-gated code lane, and
  injects each item's `text` verbatim as `<cognee_memory>` inside `<cognee_memories>`.
  The separate session-layers request and its `<agent_guidance>`, `<trace_lessons>`
  and `<session_memory>` blocks are gone, as is the JSON `<graph_memory>` block and
  its per-entry clipping; under multi-scope recall the per-scope `<agent_memory>` /
  `<user_memory>` / `<company_memory>` labels give way to one `<cognee_memory>` per
  dataset. `recallSessionLayers` is now a parsed no-op, kept so existing configs
  keep validating. Against a pre-1.6.0 server the item holds the bare retrieval
  context and is injected the same way.
- **`memory_search` searches the knowledge graph only.** The tool used to accept
  `corpus=sessions` / `all` and, when a session id resolved, add a second recall
  over the session-cache layers (`scope: ["session","trace","session_context"]`,
  `context_profile: "agent"`), tagging those hits `scope: "session"`. Those layers
  are noise when searched, so the tool now matches the prompt-time recall: one
  explicit `scope: ["graph"]` request per recall dataset, nothing else. `corpus`
  accepts `memory` | `all` (synonyms) | `wiki` (no results); `sessions` is gone
  from the schema and any unknown value falls back to `all`. Hits are always
  `scope: "graph"`, `cognee://session/…` references are no longer produced, and
  `memory_get` rejects them as it does any non-reference path. Session capture,
  bridging (`/improve`) and sync are unchanged — sessions are still written, just
  never searched.

### Fixed
- **Fresh installs against cognee 1.6.0 could not mint their owner API key
  (SDK-740).** cognee 1.6.0 stopped baking `default_password` into the default user:
  the server creates that user at startup only when `DEFAULT_USER_PASSWORD` is set,
  sets the password once, and never rewrites a stored one, so the login the key
  bootstrap relies on answered `400 "This user does not have a password"`. The
  bootstrap script (`ensure_and_boot.py`) now starts the server with
  `DEFAULT_USER_EMAIL=default_user@example.com` and
  `DEFAULT_USER_PASSWORD=default_password` (the literals every Cognee plugin shares,
  since they all share one server and one database), `setdefault` so an operator's
  own `DEFAULT_USER_*` export wins. The plugin's `username`/`password` still select
  which user it logs in as and are never forwarded to the server; a non-default user
  must already exist. Against a server the plugin did not start, the two 400 answers
  now produce an actionable message (start the server with `DEFAULT_USER_PASSWORD`
  matching the plugin's password, or set `COGNEE_API_KEY`). The README, the
  manifest's `password` description and the falkor skill document it.

## [2026.9.8]

### Fixed
- **Hotfix: the uv-less install fallback refuses a host python older than 3.10
  (SDK-617).** The bootstrap script (`~/.cognee-plugin/ensure_and_boot.py`) runs
  under the system `python3` — `/usr/bin/python3`, which is 3.9.6 on macOS with
  the Xcode Command Line Tools — and that is fine: it only needs the standard
  library, and cognee runs in the uv-managed Python 3.12 venv it builds. But when
  uv is unavailable and cannot be downloaded, the fallback `python3 -m venv`
  inherits the host version, and a 3.9 venv can never hold cognee. The script
  now refuses in that case and, since it daemonizes with its output closed,
  records the reason in `~/.cognee-plugin/.venv-error.json`; the gateway's
  "server did not become ready" warning quotes it (`readBootError`). The marker
  is cleared once an install succeeds. README gains a Requirements section:
  Python 3.9+ for the bootstrap, 3.10+ only for the uv-less fallback, none in
  cloud mode. Shipped in place, without a version bump.
- **Repo indexing submitted the repository under a field the server had stopped
  reading, so every index 400'd ([#420](https://github.com/topoteretes/cognee-integrations/issues/420)).**
  cognee 1.5.4 renamed the form field that carries the repository spec on
  `POST /api/v1/remember` with `content_type=code` from `repositories` to
  `raw_data`. `openclaw cognee index-repo` still sent the old name, and an unrecognised multipart part is
  dropped by the server rather than refused — so each request arrived naming no
  repository at all and came back `HTTP 400: content_type='code' requires at least
  one repository path or git URL in 'raw_data'`. Local paths and git URLs failed
  alike. The spec now goes in `raw_data`.
- **Every failed index blamed the server version.** The branch matched `/\(400\)/`
  — *any* 400 at all — and appended "code indexing requires Cognee >= 1.5.3" to it,
  so a bad path, a disabled local-path setting and the field mismatch above all
  printed the same misleading advice. It now matches the server's "Unsupported
  content_type" wording only.

### Changed
- **Per-prompt recall waits long enough for growing graphs.** `recallTimeoutMs`
  (per recall call) and `recallBudgetMs` (whole recall step) default to `10000` and
  `12000`, up from `2500` and `4000`, matching the Claude Code and Codex plugins.
  Graph search time grows with the dataset and with the round trip to a remote
  (cloud) server, and a call that overruns its timeout contributes nothing, so the
  old caps could silently drop graph memory from recall once a graph got large. Both remain configurable; the cheap scopes are unaffected,
  so a fast prompt is not slower.
- **Bundled server pin bumped to `cognee==1.5.4`** (`src/server.ts`; the venv upgrades
  on next boot), and `cognee-docker-compose.yaml` now uses `cognee/cognee:1.5.4`.
  Required by the field rename above, and it re-aligns this plugin with the
  claude-code/codex/antigravity plugins, which pin 1.5.4 and share the same
  `~/.cognee-plugin/venv`: while the pins differed, a cold boot by either side
  flipped the venv to its own version and re-ran that release's migrations over a
  database the other had written. The drift guard that exists to catch exactly this
  (`integrations/tests/tests/unit/test_cognee_pin.py`) was resolving `src/server.ts`
  one directory too high, so it had been passing as an expected failure on a missing
  file rather than on the pin; the path is fixed and the pin agreement is now
  enforced.

## [2026.9.2]

### Fixed
- **Memory steer moved off the removed `before_agent_start` hook.** OpenClaw deprecated
  `before_agent_start` in 2026.7 ("Use before_model_resolve and before_prompt_build") and
  dropped it from the plugin hook API in 2026.9.1-beta.1, so ClawHub's
  `clawhub package validate --openclaw-version 2026.9.1-beta.1` flagged the steer
  registration in `dist/src/plugin.js`. The steer now rides `before_prompt_build`, which
  carries the same `appendSystemContext` result field on every supported OpenClaw version
  and is likewise a prompt-injection hook, so `allowPromptInjection` behaviour is unchanged.
  OpenClaw concatenates system context across all `before_prompt_build` results, so the
  steer, auto-recall and QA capture coexist as separate handlers.

## [2026.8.27]

Parity release: brings the OpenClaw plugin up to the claude-code/codex integrations on
every user-facing memory operation, expressed the way OpenClaw agents consume
capabilities — as tools, not slash-command skills.

### Added
- **Native memory tools `memory_search` / `memory_get`.** OpenClaw's memory slot carries a
  tool contract and the bundled `active-memory` extension allow-lists exactly these two
  names; with Cognee owning the slot and registering nothing, it failed with "No callable
  tools remain". Both are now Cognee-backed: search fans out over the configured scopes
  (`corpus=memory`), the session cache (`corpus=sessions`) or both (`all`), returning
  `cognee://` references; get resolves a reference to full text with provenance or reads a
  bounded excerpt of `MEMORY.md` / `memory/*.md`. Unavailability is signalled with
  `disabled: true`, never thrown. Declared in `contracts.tools`.
- **`memory_forget` tool** — user-directed, per-document deletion ("forget what we said
  about tennis"). Two-phase: `action=find` lists candidates with raw-text previews, session
  ids and matched terms; `action=forget` deletes only the listed `dataIds`, one
  `POST /forget` each, and only with `confirm: true`. Whole-dataset and everything wipes
  stay CLI-only by construction.
- **`memory_switch_dataset` tool** — move **one conversation** (keyed by `sessionKey`,
  falling back to `sessionId`) to another dataset: `list` / `current` / `switch` / `reset`.
  A switch syncs the current session strictly (`force` to override), ensures the target and
  caches its id, then repoints capture, the agent/single recall scope, the session-layer
  lane and session-end `improve` under a fresh Cognee session id (`open_claw_<id>__N`).
  `company`/`user` scopes and memory-file sync are untouched. A session retired with
  `force` after a failed sync is recorded and re-synced into its own dataset at session
  end (and by `reset`, which refuses without `force` while any retired session is still
  unsynced), so the escape hatch defers the sync instead of dropping turns. Overrides
  persist in `~/.openclaw/memory/cognee/dataset-overrides.json`.
- **Code graph.** `openclaw cognee index-repo <path|url> [--dataset] [--index-vectors]
  [--wait <s>]` indexes a repository into a deterministic code graph (enola pipeline, one
  `codebase-<repo>-<digest>` dataset per repo); `memory_code_search` answers structural
  questions exactly (`query_facts`, `explore`, `traverse`, `find_path`, `impact_analysis`,
  `delta`); an additive `code` recall lane fires only when a prompt names an
  identifier-shaped token **and** a code graph is indexed or listed in `codeDatasets`.
  Autoindex and per-turn re-ingest are intentionally not ported — OpenClaw agents are
  rarely launched inside a checkout. Indexed repos are recorded in
  `~/.openclaw/memory/cognee/code-graphs.json`.
- **Recall session layers.** With `dataset_ids` + `search_type` in the request the
  server's default `auto` scope is graph-only, so cached Q&A turns, tool-call lessons and
  distilled agent guidance never reached the prompt. Recall now runs one extra call with
  `scope: ["session","trace","session_context"]` alongside the graph lanes and injects each
  layer as its own block (`<agent_guidance>`, `<trace_lessons>`, `<session_memory>`);
  `memory_search corpus=sessions` uses the same scope. New `recallSessionLayers` flag.
- **Memory steer.** One cached system-prompt line per real agent run (`appendSystemContext`)
  asserting Cognee as the preferred, authoritative long-term memory and naming the memory
  tools — the counterpart of claude-code's `COGNEE_PREFER_MEMORY`. Skipped on harness-noise
  turns. New `memorySteer` / `memorySteerText`.
- **Version display + npm update hint.** `openclaw cognee version` and a version-led
  `openclaw cognee status`, with an "update available" hint when the rate-limited,
  fail-silent background npm check (cached in `update-check.json`; `COGNEE_UPDATE_CHECK`,
  `COGNEE_UPDATE_CHECK_INTERVAL`) finds a newer release. `--check-updates` forces a live
  check. Based on community PR #291 by @Akshats-git.
- Config keys: `memoryTools`, `memoryForgetTool`, `datasetSwitchTool`, `codeSearchTool`,
  `codeGraphRecall`, `codeDatasets`, `recallSessionLayers`, `memorySteer`,
  `memorySteerText` — all on by default except `codeDatasets` (empty).

### Fixed
- **`client.improve()` misread the `/improve` response.** Cognee ≥ 1.4 answers with a
  per-dataset map (`{ "<dataset_uuid>": { status, pipeline_run_id } }`); the plugin read
  `.status` off the top level and logged `status=?` on every session end. The response is
  now normalized (single map unwrapped, multi-dataset summarized as `mixed`, legacy flat
  shape kept).
- **`improveOnSessionEnd` could not be set.** `resolveConfig` honoured it but
  `openclaw.plugin.json` (`additionalProperties: false`) omitted it, so valid config was
  rejected. Added to the manifest schema.

### Changed
- **Pinned Cognee server bumped to 1.5.3** (`src/server.ts`; the venv is upgraded on next
  boot) and `cognee-docker-compose.yaml` now uses `cognee/cognee:1.5.3`. Needed for
  `content_type="code"` indexing and targeted session invalidation on document delete.
- Client: `listDatasetData`, `readRawData`, `forget` by `datasetId`, `indexRepository`,
  `pipelineStatus`; `recall` accepts `scope`, `contextProfile`, `codeQuery`.
- Recall results carry the server's `source` discriminator; session-layer entries
  (question/answer, trace steps, distilled context) are rendered to text.

### Known limitations
- Sessions are never marked `completed` server-side: `mark_ended` exists in the server but
  has no HTTP route, so every integration's sessions read as `running` → `abandoned`.
  Observability only; a server-side endpoint is needed first, then the plugins will call it at session end.
- `time` provenance on `memory_search` results depends on the server populating
  `created_at`/`timestamp` in result metadata.
- State under `~/.openclaw/memory/cognee/` (dataset ids, sync indexes, dataset overrides,
  code-graph registry) is owned by one gateway process. Within a process every plugin
  instance shares one in-memory store per file; two gateway processes sharing the same
  home directory are not supported.

## [2026.8.20] and earlier

Pre-changelog releases. See the git history of `integrations/openclaw/` — notable earlier
work includes the harness-noise filter, the recall budget + shared circuit breaker,
multi-scope memory with per-agent datasets, session capture via `/remember/entry`, the
test harness with mock server and live tier, and ClawHub publication.
