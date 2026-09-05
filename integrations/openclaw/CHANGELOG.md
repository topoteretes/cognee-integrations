# Changelog

All notable changes to the **@cognee/cognee-openclaw** OpenClaw memory plugin are documented here.

The version here must match the `version` field in `package.json` (and `package-lock.json`)
and the `PLUGIN_VERSION` fallback in `src/version.ts` — a drift-guard test
(`__tests__/unit/test_version.ts`) fails when they disagree, and `openclaw cognee status`
reports an update only when the published npm version changes. Tag releases as
`openclaw-vYYYY.M.D` (matching the repo's per-plugin tag convention).

The format is based on [Keep a Changelog](https://keepachangelog.com/). Versions are
date-based (`YYYY.M.D`), matching the OpenClaw plugin ecosystem.

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
