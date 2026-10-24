/**
 * @cognee/paperclip-plugin — library surface (the host loads dist/manifest.js
 * and dist/worker.js; this is for tests and for embedding the pieces).
 */
export { manifest, DECISION_MARKER_DEFAULT, JOB_DRAIN, JOB_RECONCILE, TOOL_RECALL, TOOL_REMEMBER, ACTION_SYNC_PROJECT, ACTION_CLEANUP_SCOPE, DATA_HEALTH } from "./manifest.js";
export { createPlugin, type CreatePluginOptions, type CreatedPlugin, type PluginRuntime } from "./plugin.js";
export { resolveConfig, parseConfig, validateConfig, redactConfig, isSecretRef, DEFAULTS, type PluginConfig, type CaptureMode } from "./config.js";
export { ScopePolicy, datasetFor } from "./scope.js";
export { CogneeClient, requireUuid, type RecallItem, type DatasetDTO, type DataItemDTO, type RememberResponse } from "./cognee-client.js";
export { CogneeApiError, ConfigError, describeError } from "./errors.js";
export { InMemoryLedger, PluginDbLedger, LEDGER_TABLE, LIVE_STATUSES, PENDING_READINESS, type Ledger, type LedgerRow, type LedgerStatus, type PluginDb } from "./ledger.js";
export { Outbox, type DrainSummary, type ReconcileSummary, type CleanupSummary, type CompanyBackend, type BackendResolver } from "./outbox.js";
export { issueRecord, commentRecord, decisionRecord, isMarkedComment, stripMarker, issueLink, hashText, normalizeText, type SourceRecord, type SourceType } from "./records.js";
export { handleIssueUpdated, handleCommentCreated, syncProject } from "./capture.js";
export { recallTool, rememberDecisionTool } from "./tools.js";
export { PACKAGE_NAME, PACKAGE_VERSION, PLUGIN_ID, PINNED_SDK_VERSION, VERIFIED_COGNEE_VERSION } from "./version.js";
