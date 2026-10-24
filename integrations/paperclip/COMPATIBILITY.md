# Compatibility

What this release was built against and what was actually exercised. A row is a
tested combination, not a promise about neighbours.

## Paperclip host and SDK

| Component | Version | Notes |
|---|---|---|
| `@paperclipai/plugin-sdk` | **2026.1012.0** (pinned exactly) | The `latest` npm tag on 2026-10-23. Its `engines` requires Node >= 24.11.0. The worker and manifest are built with the SDK's own bundler presets (`createPluginBundlerPresets`, esbuild, `target: node24`). |
| `@paperclipai/shared` | 2026.1012.0 (SDK dependency) | Source of `PLUGIN_CAPABILITIES`, `PLUGIN_EVENT_TYPES`, `Issue` / `IssueComment` shapes used here. |
| Paperclip host | **not exercised** | No Paperclip server (Postgres, `paperclipai plugin install`) was available to the build. The plugin was verified with the SDK's in-memory host harness (`@paperclipai/plugin-sdk/testing`), which enforces declared capabilities and simulates events, jobs, tools, actions, config and secret resolution. Treat "installs on a clean host" as **unverified** until someone runs `npx paperclipai plugin install @cognee/paperclip-plugin` on a pinned host release and records it here. |
| Node.js | **24.11+** for the host; tested locally on **22.23.2** | The SDK imports and runs on 22 (verified); CI and the `engines` field follow the SDK's requirement. |
| Agent adapters | **not verified** | Tool delivery to a real agent run (`codex_local`, `paperclip_runner`, …) was not exercised. Until two adapters complete a cross-run recall, the release supports the harness-proven tool contract only; see the plan's milestone 3. |

## cognee backend

| Backend | Version | Verified |
|---|---|---|
| Self-hosted cognee | **1.6.5** | Live on 2026-10-23 through the harness: dataset create, background `remember`, cognify status, dataset data listing, `CHUNKS` recall with explicit `datasets` + `scope: ["graph"]`, targeted `forget` by `datasetId` + `dataId` with re-listing, 401/404 bodies. Captured bodies in [`fixtures/cognee-1.6.5/`](fixtures/cognee-1.6.5/README.md). |
| Cognee Cloud | not verified | Same `/api/v1` routes and `X-Api-Key` auth; run the plugin's **Test connection** (`onValidateConfig`) and `npm run test:live` against your tenant before relying on it. |

Observed 1.6.5 behaviours the ledger is built around:

- A background `remember` answers `{"status":"running", "dataset_id": …}` with **no document
  id**; the document is found later by its content-derived upload name
  (`<issue|comment|decision>-<sha256[:32]>`) in `GET /datasets/{id}/data`.
- A reused upload name with different content is refused with 409 only on a synchronous
  write and dropped silently on the background path; content-derived names avoid it, and a
  document that never appears becomes a reconciled **resend of the same name** (identical
  content is a server-side no-op) rather than a guess.
- `POST /forget` answers `status: "success"` even for an unknown `dataId`, so every retirement
  and cleanup re-lists the dataset and marks the row `conflict` if the document is still there.
- `POST /recall` on a dataset name that does not exist answers HTTP 404; the recall tool
  checks the dataset list first and reports "nothing remembered yet" instead of searching
  unscoped.

## Milestone 0 findings (from the plan)

| Question | Finding | Consequence |
|---|---|---|
| Does a scheduled job carry company authority? | **No.** `PluginJobContext` is `{ jobKey, runId, trigger, scheduledAt }`; no company id. | Event handlers (which do carry `event.companyId`) authorize and **snapshot** everything a later send needs into the ledger: company id, project id, dataset name, the full record text, upload name. The drain/reconcile jobs take company ids from the rows and call `ctx.config.get(companyId)` + `ctx.secrets.resolve(ref, {companyId})` per company. Looping over all companies is never used to widen authority. |
| Is the tool run context trustworthy? | The host supplies `ToolRunContext { agentId, runId, companyId, projectId }` separately from the model's `params`. | Tools take company and project from the context only; a `datasets` / `company_id` field in params is ignored (tested). Project membership of `source_issue_id` is re-checked through `ctx.issues.get(issueId, companyId)`. |
| Event payload shapes for `issue.updated` / `issue.comment.created`? | Not documented in `PLUGIN_SPEC.md`; the SDK types only guarantee the envelope (`entityId`, `entityType`, `companyId`, `payload`). | Handlers use `entityId` (plus `payload.issueId` / `payload.commentId` / `payload.comment.issueId` fallbacks) and **always re-read the source through the host**. A comment event whose payload carries no issue id is skipped with a log line; verify the real payload on the pinned host and adjust `capture.ts` if the field is named differently. |
| Issue link route format? | Not verified against a host. | Links are built from `paperclipPublicUrl` with the configurable `issueLinkTemplate` (default `{publicUrl}/issues/{issueId}`). Adjust the template once the host's route is confirmed. |
| Does `ctx.db` give atomic uniqueness? | Yes by schema: `migrations/001_memory_ledger.sql` declares `UNIQUE (company_id, source_type, source_id, revision)` and inserts use `ON CONFLICT DO NOTHING`. | Replayed and reordered events produce one row (tested with the in-memory ledger that mirrors the SQL semantics; the harness's `ctx.db` records SQL but stores nothing, so the SQL ledger is covered by statement-shape tests, not a live Postgres). |
| Dynamic installation | The SDK README limits dependable runtime installs to single-node persistent deployments with `npm` available. | Documented in the README; nothing in the plugin assumes multi-node. |

## Native connector vs plugin mode

Paperclip's experimental Cognee memory connection (Settings → Experimental → Memory
connectors) offers tool-driven `remember` / `recall` / `forget` against a Cognee Cloud
tenant with no automatic capture. This plugin is a separate installation mode: it uses its
own operator-configured secret reference and host-governed outbound HTTP, never the native
connection's credentials, and the native connection's permissions do not govern the plugin's
calls. Both can be installed side by side; the plugin's capture is scoped by its own
company/project allowlists.
