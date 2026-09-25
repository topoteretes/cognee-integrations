# Changelog

All notable changes to the **@cognee/cognee-openclaw** OpenClaw memory plugin are documented here.

The version here must match the `version` field in `package.json` (and `package-lock.json`)
and the `PLUGIN_VERSION` fallback in `src/version.ts` — a drift-guard test
(`__tests__/unit/test_version.ts`) fails when they disagree, and `openclaw cognee status`
reports an update only when the published npm version changes. Tag releases as
`openclaw-vYYYY.M.D` (matching the repo's per-plugin tag convention).

The format is based on [Keep a Changelog](https://keepachangelog.com/). Versions are
date-based (`YYYY.M.D`), matching the OpenClaw plugin ecosystem.

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
