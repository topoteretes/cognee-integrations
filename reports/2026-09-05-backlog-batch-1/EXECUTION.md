# Batch 1 execution report — 5 September 2026

Implemented the approved recommendations and merged the integration work. **All 10 selected PRs are resolved: one merged and nine closed as superseded. Eight of the 10 selected issues are closed; two remain open with concrete follow-up work.** One additional overlapping PR (#225) was closed.

The initial [audit](REPORT.md) is a historical snapshot. This report records the actions actually taken. GitHub states and check details are preserved in [execution-snapshot.json](execution-snapshot.json).

## Ten pull requests

| PR | Final state | Action |
|---|---|---|
| [#116](https://github.com/topoteretes/cognee-integrations/pull/116) | CLOSED | Closed: superseded by #202, now merged through #400. |
| [#169](https://github.com/topoteretes/cognee-integrations/pull/169) | CLOSED | Closed: rewritten against current configuration in #397, merged through #400. |
| [#175](https://github.com/topoteretes/cognee-integrations/pull/175) | CLOSED | Closed after the version guard and metadata fixes merged in #395. |
| [#183](https://github.com/topoteretes/cognee-integrations/pull/183) | CLOSED | Closed: bounded per-conversation cold-start retry rewrite in #398, merged through #400. |
| [#185](https://github.com/topoteretes/cognee-integrations/pull/185) | CLOSED | Closed in favor of maintained #285; picker functionality remains separately tracked. |
| [#186](https://github.com/topoteretes/cognee-integrations/pull/186) | CLOSED | Closed as superseded by previously merged #368. |
| [#192](https://github.com/topoteretes/cognee-integrations/pull/192) | CLOSED | Closed in favor of maintained #262; URL-switching functionality remains separately tracked. |
| [#193](https://github.com/topoteretes/cognee-integrations/pull/193) | CLOSED | Closed: event registry and compatible migration in #397, merged through #400. |
| [#202](https://github.com/topoteretes/cognee-integrations/pull/202) | MERGED | Merged with the contributor commits preserved in #400; request tests, metadata, docs and Node 20 validation added. |
| [#211](https://github.com/topoteretes/cognee-integrations/pull/211) | CLOSED | Closed: client rewrite merged in #400; authoritative server provisioning awaits cognee#4948. |

## Ten issues

| Issue | Final state | Action |
|---|---|---|
| [#135](https://github.com/topoteretes/cognee-integrations/issues/135) | CLOSED | Closed as completed by #283. |
| [#136](https://github.com/topoteretes/cognee-integrations/issues/136) | CLOSED | Closed by merged #395; the guard now covers six release manifests. |
| [#138](https://github.com/topoteretes/cognee-integrations/issues/138) | CLOSED | Closed by merged #400: canonical event registry with legacy compatibility. |
| [#171](https://github.com/topoteretes/cognee-integrations/issues/171) | CLOSED | Closed combined request. Provider/model pass-through is documented; core coverage is in cognee#4948 / #4947. Subscription OAuth token reuse was declined. |
| [#314](https://github.com/topoteretes/cognee-integrations/issues/314) | OPEN | Client merged in #400. Keep open: server extension cognee#4948 needs independent approval, merge and deployment. |
| [#319](https://github.com/topoteretes/cognee-integrations/issues/319) | OPEN | Implementation merged in #400. Keep open: native CLI end-to-end validation remains unresolved; both host versions stalled at initialization. |
| [#320](https://github.com/topoteretes/cognee-integrations/issues/320) | CLOSED | Closed: prompt identity probes removed, argv keys removed, recall bounded, log rotation serialized. |
| [#332](https://github.com/topoteretes/cognee-integrations/issues/332) | CLOSED | Closed: automatic-capture opt-out, tool/path filtering and pre-buffer/upload redaction. |
| [#335](https://github.com/topoteretes/cognee-integrations/issues/335) | CLOSED | Closed as obsolete for the supported HTTP-server configuration. Dormant direct-SDK setup still needs a timeout or removal before restoration. |
| [#350](https://github.com/topoteretes/cognee-integrations/issues/350) | CLOSED | Closed: optional lifecycle 404 distinguished from authentication and server errors. |

## What landed

[PR #395](https://github.com/topoteretes/cognee-integrations/pull/395) merged as `393eeac05f6ea39695ea6a9ab5ced828d02e2560`. It removed prompt-path identity requests and credential-bearing watcher arguments, corrected inventory drift and added release-version validation.

[PR #400](https://github.com/topoteretes/cognee-integrations/pull/400) merged as `fe210608b219ffb3fa7e6bbd9e43d11c0a5b113f`. Its merge commit preserves the original commits from **#202 and #396–#399**; GitHub marks all of those PRs merged.

- **Shared Python hooks:** elapsed-time recall bounds; serialized log rotation; capture opt-out, allowlists, sensitive-path filtering and redaction before buffering/upload; optional lifecycle 404 support; current configuration tests; canonical event names with legacy compatibility.
- **OpenClaw:** first-recall retries belong to each native conversation and share one total deadline. The existing warm-up behavior is retained.
- **n8n:** Recall and Remember resources updated in the original contribution, with multipart/request regressions, documentation and consistent release metadata.
- **OpenCode:** native session identities; per-turn scoped recall; typed completed QA/tool capture; durable deduplicating outbox; lifecycle and heartbeat; setup/index/status/health commands; plugin API compatibility tests. The host entry module exposes only the plugin initializer; the HTTP client has its own package subpath.
- **Project memory:** opt-in project tags and companion routing are pinned per session. Companion selection requires permission attestation from the authoritative remote server. Unsupported project tags retain capture in the queue; unverified companions fall back to the primary.
- **Antigravity:** this integration merged independently during the audit. Its host-specific behavior was preserved, and the shared hardening was ported into it. Version validation now covers six release manifests, including Antigravity and OpenCode.

Maintained integration packages were retained. Duplicate inventory records and superseded PRs were removed. No evidence justified deleting an integration package. Registry releases and backend deployments were not performed.

## Validation and remaining gates

| Area | Evidence |
|---|---|
| Shared Python hooks | **2,379 passed**, 186 skipped, 53 deselected, 2 expected failures; Linux CI passed on the merged head. |
| OpenClaw | **397 tests / 33 suites** and typecheck passed; integration CI passed. |
| n8n | Request regressions, build, lint and typecheck passed under Node 20; integration CI passed. |
| OpenCode | **8 tests** passed under Node 20, including abandoned-lock recovery across concurrent processes; compatibility CI passed for APIs **1.18.28 and 1.18.29**. |
| Metadata / style | Six release manifests agree with inventory; **7 guard tests** and repository integration Ruff checks passed. |
| Windows | Still running at merge. The user explicitly instructed proceeding without waiting. Earlier #395/#396 Windows runs passed; that is not a substitute for the final combined run. |
| Automated review | Earlier #395/#396 posted reviews reported no blocking findings despite advisory action turn-limit failures. #400's advisory review was still running at merge. |
| Core companion/tag extension | [cognee#4948](https://github.com/topoteretes/cognee/pull/4948) is implemented and published. **96 focused tests** passed; the 13 watermark cases and 5 new feature cases passed after follow-up fixture/import fixes. New modules pass Ruff. |

**Core remains unmerged.** Its branch requires one independent approving review. Repository-wide quality CI also fails on existing lint debt (over 10,000 local findings); baseline comparison identified and corrected lint introduced by this change. The stale dependency lock was refreshed without adding, removing or changing package versions. Existing main's [Test Suites run](https://github.com/topoteretes/cognee/actions/runs/33956404592) had already failed. Full backend CI is recorded on the PR; no branch protections were bypassed.

The core PR references existing Linear ticket **SDK-303**, and its Linear reference check passed. Orca's saved Linear credential could not be decrypted; the connected Linear app supplied the ticket context. No Linear state or assignee was changed.

**OpenCode native verification remains incomplete.** Isolated native CLI tests of 1.18.29 stalled at initialization with both the plugin and an empty plugin list, before any mock backend/model request. A 1.18.28 attempt with a Git workspace and snapshots disabled also stalled. The entry-point loader bug found during inspection was fixed, but API/contract tests alone do not prove an end-to-end native host session. Issue #319 remains open for that acceptance check.

Companion permissions are a provisioning snapshot, not continuous inheritance. Later sharing changes require explicit reconciliation. The endpoint is owner-only, and server tag binding uses the existing single-worker session lock. OpenCode holds ambiguous writes for reconciliation; an entry outside the server's recent-session window can require operator resolution. These limitations are documented in the implementations.
