# Changelog

All notable changes to the **@cognee/cognee-mastra** package are documented here.

The version here must match the `version` field in `package.json` and the
`VERSION` constant in `src/version.ts`. This package uses semver (unlike the
date-versioned OpenClaw/OpenCode plugins in this monorepo) because it carries
a `@mastra/core` peer range and lives in Mastra's semver-based ecosystem. Tag
releases as `mastra-v<version>` per the repo's tag-per-package convention.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0]

Initial release. A TypeScript package exposing cognee as (a) an automatic
memory layer for Mastra agents via input/output processors and (b) explicit
recall tools, entirely over cognee's plain HTTP API — no cognee SDK
dependency.

### Added

- **`CogneeClient`** (`src/client.ts`) — the package's only network surface.
  Implements `health`, `login`/JWT-with-cached-token, `ensureDataset`/
  `listDatasets`/`datasetStatus`, `remember`/`add`/`cognify`, `rememberEntry`,
  `recall` (with a normalized `RecallHit[]` result), `forget` (`everything:
  true` hard-blocked), and `improve`. Retry (3×, exponential backoff base 3s)
  on 5xx/429/network failures, disabled whenever a per-call `timeoutMs` is
  set; `X-Api-Key` when configured, JWT login with one silent re-login on a
  401 otherwise; `AbortSignal` timeouts on every call; an in-memory
  `ensureDataset` name→id cache for the process lifetime. Ported from
  `integrations/openclaw`'s `CogneeHttpClient` auth/retry/timeout semantics
  rather than imported, to avoid pulling OpenClaw's plugin surface and peer
  dependency into a Mastra package.
- **`src/capabilities.ts`** — lazy, one-shot-per-base-URL runtime probing for
  the four endpoint-shape questions a cognee server's exact version otherwise
  leaves open: `/recall` vs. the legacy `/search`, `/remember` vs. `/add` +
  `/cognify`, whether `/remember/entry` exists, and which prefix the login
  route is mounted at. Each probe piggybacks on the first real request that
  needs it — a 404 costs one extra round trip, once, ever, per base URL; a
  server that supports the modern route pays nothing extra.
- **`resolveConfig()`** (`src/config.ts`) — the single place in the package
  that reads `process.env`, resolving every field in `CogneeMastraConfig`
  as explicit argument → environment variable → default. Never throws for a
  missing credential. `redactConfigForLogging()` masks `apiKey`/
  `auth.password` for any caller that logs the resolved config.
- **`src/scope.ts`** — Mastra thread/resource id → cognee dataset/session/tag
  mapping, both `scope` modes (`"tagged"` default, `"dataset-per-resource"`).
  Ids are percent-encoded (`sanitizeId`) before being embedded in a tag or
  dataset name, so ids containing `:`, spaces, or `.` stay collision-free and
  never produce a dataset name cognee's server-side validation rejects.
- **`src/format.ts`** — pure, dependency-free text formatting: word-boundary
  message truncation (`truncateAtWordBoundary`), Mastra message → cognee
  ingestible text (`messageToText`), and recall hits → the injected
  system-message context block (`formatContextBlock`, returns `null` — not an
  empty block — on zero usable hits).
- **`CircuitBreaker`** (`src/breaker.ts`) — file-backed circuit breaker for
  the recall path (5 consecutive failures → 120s cooldown), state at
  `~/.cognee-mastra/recall-breaker.json`. A missing, unreadable, or corrupt
  state file degrades to closed rather than failing open.
- **`CogneeApiError`** (`src/errors.ts`) — the one error type the client
  throws for a non-2xx response or a network/timeout failure (`status: 0` in
  the latter case), plus `isRetryable(status)` as the single source of truth
  for the client's own retry loop and its tests.
- **`CogneeInputProcessor` / `CogneeOutputProcessor` / `createCogneeProcessors()`**
  (`src/processors.ts`) — the automatic half of the dual surface. Input
  recalls before the model call (`search_type: CHUNKS`, `only_context:
  true`) under a per-call `timeoutMs` and an overall `budgetMs` wall-clock
  cap, consulting the circuit breaker first; output persists the turn via
  `rememberEntry()` once, fire-and-forget, honouring `write.mode`. Both fail
  open on every error path — a cognee failure never propagates to the agent
  turn. Built on `@mastra/core`'s `Processor` interface (`id`,
  `processInput`, `processOutputResult`) and
  `parseMemoryRequestContext(requestContext)` for `thread`/`resourceId`
  plumbing.
- **`withCognee(agentConfig, config)`** (`src/with-cognee.ts`) — merges
  cognee's processors into an existing Mastra agent config, appending to
  whatever `inputProcessors`/`outputProcessors` (array or function form) the
  caller already has, rather than replacing them.
- **`createCogneeSearchTool` / `createCogneeAskTool` / `createCogneeRememberTool` /
  `createCogneeForgetTool` / `createCogneeTools()`** (`src/tools.ts`) — the
  explicit, model-driven half of the dual surface. `cognee_forget` is
  destructive (`memory_only: true` always forced) and is only present in
  `createCogneeTools()`'s returned record when `tools.enableForget === true`.
  Every tool's `execute` returns a typed `{ error: string }` on failure
  rather than throwing, and is wrapped in an observability span.
- **`src/types.ts`** — the full `CogneeMastraConfig` surface plus wire DTOs
  for every cognee endpoint this package calls, checked against the pinned
  cognee server source (routers, response models, the `QAEntry` shape) where
  the build spec's evidence was thin, not just against the spec text itself.
- Unit tests (`__tests__/unit/`) for `config`, `scope`, `format`, and
  `breaker` (fake-clocked open/half-open/close). End-to-end tests
  (`__tests__/e2e/`) for the processors and tools against an in-process real
  `node:http` mock cognee server (`__tests__/test-utils/mock-cognee.ts`), not
  a fetch monkey-patch, so the client's actual retry/timeout/abort behaviour
  is exercised.

### Verified against

- `cognee/cognee:main` reporting `1.5.4-local`, via the opt-in live smoke
  test (`COGNEE_LIVE=1 npm run test:live`): health → dataset → remember →
  cognify poll → recall → forget, all green. See the Compatibility section
  of `README.md` for the resolved capability probes and the observed
  write→recallable delay.

### Known limitations

- `scope: "tagged"` (the default) is not a tenant boundary — see the
  Scoping section of `README.md`. Real isolation needs
  `scope: "dataset-per-resource"` plus per-tenant cognee credentials.
- The circuit-breaker state file is written without a lock; two processes
  sharing one `~/.cognee-mastra/recall-breaker.json` can race each other's
  counters. Harmless in the fail-open direction, but not a coordinated
  breaker.
- Capability probes are cached per base URL for the life of the process; a
  server upgraded in place keeps the old resolution until restart.

[0.1.0]: https://github.com/topoteretes/cognee-integrations/tree/main/integrations/mastra
