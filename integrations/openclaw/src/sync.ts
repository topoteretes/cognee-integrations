import type { CogneeHttpClient } from "./client.js";
import type { CogneePluginConfig, MemoryFile, MemoryScope, ScopedSyncIndexes, SyncIndex, SyncResult } from "./types.js";
import { loadDatasetState, saveDatasetState, saveScopedSyncIndexes, saveSyncIndex } from "./persistence.js";
import { datasetNameForScope, routeFileToScope } from "./scope.js";

// ---------------------------------------------------------------------------
// Single-scope sync
// ---------------------------------------------------------------------------

export async function syncFiles(
  client: CogneeHttpClient,
  changedFiles: MemoryFile[],
  fullFiles: MemoryFile[],
  syncIndex: SyncIndex,
  cfg: Required<CogneePluginConfig>,
  logger: { info?: (msg: string) => void; warn?: (msg: string) => void },
  overrideDatasetName?: string,
  onNewDataset?: (datasetId: string) => Promise<void>,
): Promise<SyncResult & { datasetId?: string }> {
  const result: SyncResult = { added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 };
  const dsName = overrideDatasetName || cfg.datasetName;
  let datasetId = syncIndex.datasetId;
  let needsCognify = false;

  for (const file of changedFiles) {
    const existing = syncIndex.entries[file.path];

    if (existing && existing.hash === file.hash) {
      result.skipped++;
      continue;
    }

    const dataWithMetadata = `# ${file.path}\n\n${file.content}\n\n---\nMetadata: ${JSON.stringify({ path: file.path, source: "memory" })}`;

    try {
      if (existing?.dataId && datasetId) {
        try {
          const updateResponse = await client.update({
            dataId: existing.dataId,
            datasetId,
            data: dataWithMetadata,
          });
          const newDataId = updateResponse.dataId;
          if (!newDataId) {
            logger.warn?.(`cognee-openclaw: update for ${file.path} succeeded but could not resolve new data_id`);
          }
          syncIndex.entries[file.path] = { hash: file.hash, dataId: newDataId };
          syncIndex.datasetId = datasetId;
          syncIndex.datasetName = dsName;
          result.updated++;
          logger.info?.(`cognee-openclaw: updated ${file.path}`);
          continue;
        } catch (updateError) {
          const errorMsg = updateError instanceof Error ? updateError.message : String(updateError);
          if (errorMsg.includes("404") || errorMsg.includes("409") || errorMsg.includes("not found")) {
            logger.info?.(`cognee-openclaw: update failed for ${file.path}, falling back to add`);
            delete existing.dataId;
          } else {
            throw updateError;
          }
        }
      }

      const response = await client.add({ data: dataWithMetadata, datasetName: dsName, datasetId });

      if (response.datasetId && response.datasetId !== datasetId) {
        datasetId = response.datasetId;
        const state = await loadDatasetState();
        state[dsName] = response.datasetId;
        await saveDatasetState(state);
        await onNewDataset?.(response.datasetId);
      }

      syncIndex.entries[file.path] = { hash: file.hash, dataId: response.dataId };
      syncIndex.datasetId = datasetId;
      syncIndex.datasetName = dsName;
      needsCognify = true;
      result.added++;
      logger.info?.(`cognee-openclaw: added ${file.path}`);
    } catch (error) {
      result.errors++;
      logger.warn?.(`cognee-openclaw: failed to sync ${file.path}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  // Handle deletions
  const currentPaths = new Set(fullFiles.map(f => f.path));
  for (const [path, entry] of Object.entries(syncIndex.entries)) {
    if (!currentPaths.has(path) && entry.dataId && datasetId) {
      const deleteResult = await client.delete({ dataId: entry.dataId, datasetId, mode: cfg.deleteMode });
      if (deleteResult.deleted) {
        result.deleted++;
        delete syncIndex.entries[path];
        logger.info?.(`cognee-openclaw: deleted ${path}`);
      } else {
        const isNotFound = deleteResult.error && (
          deleteResult.error.includes("404") || deleteResult.error.includes("409") || deleteResult.error.includes("not found")
        );
        if (isNotFound) {
          result.deleted++;
          delete syncIndex.entries[path];
          logger.info?.(`cognee-openclaw: deleted ${path} (already removed from Cognee)`);
        } else {
          result.errors++;
          logger.warn?.(`cognee-openclaw: failed to delete ${path}${deleteResult.error ? `: ${deleteResult.error}` : ""}`);
        }
      }
    }
  }

  // Fix #9: Only trigger memify AFTER polling cognify to completion
  if (needsCognify && cfg.autoCognify && datasetId) {
    try {
      await client.cognify({ datasetIds: [datasetId] });
      logger.info?.("cognee-openclaw: cognify dispatched");

      if (cfg.autoMemify) {
        // Poll for cognify completion before triggering memify
        const memifyDatasetId = datasetId;
        await waitForCognifyThenMemify(client, memifyDatasetId, logger);
      }
    } catch (error) {
      logger.warn?.(`cognee-openclaw: cognify failed: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  await saveSyncIndex(syncIndex);
  return { ...result, datasetId };
}

// ---------------------------------------------------------------------------
// Multi-scope sync
// ---------------------------------------------------------------------------

export async function syncFilesScoped(
  client: CogneeHttpClient,
  changedFiles: MemoryFile[],
  fullFiles: MemoryFile[],
  scopedIndexes: ScopedSyncIndexes,
  cfg: Required<CogneePluginConfig>,
  logger: { info?: (msg: string) => void; warn?: (msg: string) => void },
  onNewDataset?: (datasetId: string, scope: MemoryScope) => Promise<void>,
): Promise<SyncResult & { datasetIds: Record<MemoryScope, string | undefined> }> {
  const totalResult: SyncResult = { added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 };
  const datasetIds: Record<MemoryScope, string | undefined> = { company: undefined, user: undefined, agent: undefined };

  // Group changed files by scope
  const changedByScope = new Map<MemoryScope, MemoryFile[]>();
  for (const file of changedFiles) {
    const scope = routeFileToScope(file.path, cfg.scopeRouting, cfg.defaultWriteScope);
    const list = changedByScope.get(scope) ?? [];
    list.push(file);
    changedByScope.set(scope, list);
  }

  // Group all files by scope
  const fullByScope = new Map<MemoryScope, MemoryFile[]>();
  for (const file of fullFiles) {
    const scope = routeFileToScope(file.path, cfg.scopeRouting, cfg.defaultWriteScope);
    const list = fullByScope.get(scope) ?? [];
    list.push(file);
    fullByScope.set(scope, list);
  }

  // Determine which scopes need processing
  const allScopes = new Set<MemoryScope>([
    ...changedByScope.keys(),
    ...(Object.keys(scopedIndexes) as MemoryScope[]),
  ]);

  for (const scope of allScopes) {
    const dsName = datasetNameForScope(scope, cfg);
    const scopeChanged = changedByScope.get(scope) ?? [];
    const scopeFull = fullByScope.get(scope) ?? [];

    if (!scopedIndexes[scope]) {
      scopedIndexes[scope] = { entries: {} };
    }
    const scopeIndex = scopedIndexes[scope]!;

    const currentPaths = new Set(scopeFull.map(f => f.path));
    const hasDeletedFiles = Object.keys(scopeIndex.entries).some(p => !currentPaths.has(p));

    if (scopeChanged.length === 0 && !hasDeletedFiles) continue;

    logger.info?.(`cognee-openclaw: [${scope}] syncing ${scopeChanged.length} changed file(s) to dataset "${dsName}"${hasDeletedFiles ? " + deletions" : ""}`);

    const result = await syncFiles(
      client, scopeChanged, scopeFull, scopeIndex, cfg, logger, dsName,
      onNewDataset ? (dsId) => onNewDataset(dsId, scope) : undefined,
    );
    totalResult.added += result.added;
    totalResult.updated += result.updated;
    totalResult.skipped += result.skipped;
    totalResult.errors += result.errors;
    totalResult.deleted += result.deleted;
    datasetIds[scope] = result.datasetId;
  }

  await saveScopedSyncIndexes(scopedIndexes);
  return { ...totalResult, datasetIds };
}

// ---------------------------------------------------------------------------
// Fix #9: Poll cognify status before triggering memify
// ---------------------------------------------------------------------------

/** Exported so tests can override */
export let COGNIFY_POLL_INTERVAL_MS = 5_000;
const COGNIFY_POLL_MAX_ATTEMPTS = 60; // 5 min max

/** Allow tests to adjust the poll interval */
export function _setPollInterval(ms: number): void { COGNIFY_POLL_INTERVAL_MS = ms; }

async function waitForCognifyThenMemify(
  client: CogneeHttpClient,
  datasetId: string,
  logger: { info?: (msg: string) => void; warn?: (msg: string) => void },
): Promise<void> {
  for (let attempt = 0; attempt < COGNIFY_POLL_MAX_ATTEMPTS; attempt++) {
    // Wait before polling (skip on first attempt to check immediately)
    if (attempt > 0) {
      await new Promise(r => setTimeout(r, COGNIFY_POLL_INTERVAL_MS));
    }

    try {
      const status = await client.datasetStatus(datasetId);
      if (status === "completed" || status === "COMPLETED") {
        try {
          await client.memify({ datasetIds: [datasetId] });
          logger.info?.("cognee-openclaw: memify dispatched after cognify completed");
        } catch (error) {
          logger.warn?.(`cognee-openclaw: memify failed: ${error instanceof Error ? error.message : String(error)}`);
        }
        return;
      }
      if (status === "failed" || status === "FAILED" || status === "error") {
        logger.warn?.(`cognee-openclaw: cognify failed (status: ${status}), skipping memify`);
        return;
      }
      if (status === "unknown") {
        // Endpoint exists but doesn't track this operation — fall back to optimistic memify
        logger.warn?.("cognee-openclaw: could not poll cognify status, running memify optimistically");
        try {
          await client.memify({ datasetIds: [datasetId] });
          logger.info?.("cognee-openclaw: memify dispatched (optimistic)");
        } catch (error) {
          logger.warn?.(`cognee-openclaw: memify failed: ${error instanceof Error ? error.message : String(error)}`);
        }
        return;
      }
      // Still running, continue polling
    } catch {
      // Status endpoint may not exist or may fail — fall back to fire-and-forget
      logger.warn?.("cognee-openclaw: could not poll cognify status, running memify optimistically");
      try {
        await client.memify({ datasetIds: [datasetId] });
        logger.info?.("cognee-openclaw: memify dispatched (optimistic)");
      } catch (error) {
        logger.warn?.(`cognee-openclaw: memify failed: ${error instanceof Error ? error.message : String(error)}`);
      }
      return;
    }
  }

  logger.warn?.("cognee-openclaw: cognify polling timed out, skipping memify");
}
