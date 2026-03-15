// ---------------------------------------------------------------------------
// @cognee/cognee-openclaw — main entry point
//
// Fix #1: The plugin is now split into focused modules under src/.
// This file is a thin re-export barrel for the public API.
// ---------------------------------------------------------------------------

export { default } from "./src/plugin.js";

// Client (shared with skills plugin — Fix #2)
export { CogneeHttpClient } from "./src/client.js";

// Sync logic
export { syncFiles, syncFilesScoped } from "./src/sync.js";

// Scope utilities
export { matchGlob, routeFileToScope, datasetNameForScope, isMultiScopeEnabled } from "./src/scope.js";

// Config
export { resolveConfig } from "./src/config.js";

// Files
export { collectMemoryFiles, hashText } from "./src/files.js";

// Persistence
export {
  loadDatasetState,
  saveDatasetState,
  loadSyncIndex,
  saveSyncIndex,
  loadScopedSyncIndexes,
  saveScopedSyncIndexes,
  migrateLegacyIndex,
} from "./src/persistence.js";

// Types
export type {
  CogneeDeleteMode,
  CogneePluginConfig,
  CogneeSearchType,
  CogneeSearchResult,
  MemoryFile,
  MemoryScope,
  ScopeRoute,
  ScopedSyncIndexes,
  SyncIndex,
  SyncResult,
} from "./src/types.js";
