import type { CogneeHttpClient } from "./client.js";
import type { CogneePluginConfig, MemoryFile, MemoryScope, ScopedSyncIndexes, SyncIndex, SyncResult } from "./types.js";
import { loadDatasetState, saveDatasetState, saveScopedSyncIndexes, saveSyncIndex } from "./persistence.js";
import { datasetNameForScope, routeFileToScope } from "./scope.js";

// ---------------------------------------------------------------------------
// Single-scope sync
//
// Cognee 1.0.3 introduced /remember as the recommended ingest path: one
// multipart call uploads a batch of files and the server runs add+cognify
// (and improve) end-to-end, returning per-file data_ids. We use it for new
// files and keep PATCH /update for changed files (remember has no update
// counterpart, and the existing 1.0.3 update endpoint already triggers a
// follow-up cognify internally, so the fan-out is the same).
// ---------------------------------------------------------------------------

export async function syncFiles(
  client: CogneeHttpClient,
  changedFiles: MemoryFile[],
  fullFiles: MemoryFile[],
  syncIndex: SyncIndex,
  cfg: Required<CogneePluginConfig>,
  logger: { info?: (msg: string) => void; warn?: (msg: string) => void },
  overrideDatasetName?: string,
): Promise<SyncResult & { datasetId?: string }> {
  const result: SyncResult = { added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 };
  const dsName = overrideDatasetName || cfg.datasetName;
  let datasetId = syncIndex.datasetId;

  // Partition changed files into "needs add" vs "needs update".
  // We process updates per-file (PATCH /update) and batch all adds into a
  // single /remember call at the end of the loop.
  const toAdd: MemoryFile[] = [];

  for (const file of changedFiles) {
    const existing = syncIndex.entries[file.path];

    if (existing && existing.hash === file.hash) {
      result.skipped++;
      continue;
    }

    if (existing?.dataId && datasetId) {
      const dataWithMetadata = wrapWithMetadata(file);
      try {
        const updateResponse = await client.update({
          dataId: existing.dataId,
          datasetId,
          data: dataWithMetadata,
          filePath: file.path,
          datasetName: dsName,
        });
        const newDataId = updateResponse.dataId;
        if (!newDataId) {
          logger.warn?.(`cognee-openclaw: update for ${file.path} succeeded but could not resolve new data_id`);
        }
        syncIndex.entries[file.path] = { hash: file.hash, dataId: newDataId };
        syncIndex.datasetId = datasetId;
        syncIndex.datasetName = dsName;
        result.updated++;
        logger.info?.(`cognee-openclaw: updated ${file.path} (newDataId=${newDataId})`);
        continue;
      } catch (updateError) {
        const errorMsg = updateError instanceof Error ? updateError.message : String(updateError);
        if (errorMsg.includes("404") || errorMsg.includes("409") || errorMsg.includes("not found")) {
          logger.info?.(`cognee-openclaw: update failed for ${file.path}, falling back to remember`);
          // fall through to add path
        } else {
          result.errors++;
          logger.warn?.(`cognee-openclaw: failed to sync ${file.path}: ${errorMsg}`);
          continue;
        }
      }
    }

    toAdd.push(file);
  }

  if (toAdd.length > 0) {
    try {
      const rememberResponse = await client.remember({
        files: toAdd.map((f) => ({ filePath: f.path, data: wrapWithMetadata(f) })),
        datasetName: dsName,
        datasetId,
      });

      if (rememberResponse.datasetId && rememberResponse.datasetId !== datasetId) {
        datasetId = rememberResponse.datasetId;
        const state = await loadDatasetState();
        state[dsName] = rememberResponse.datasetId;
        await saveDatasetState(state);
      }

      const itemsByPath = new Map<string, string | undefined>();
      for (const item of rememberResponse.items) {
        itemsByPath.set(item.filePath, item.dataId);
      }

      for (const file of toAdd) {
        const dataId = itemsByPath.get(file.path);
        syncIndex.entries[file.path] = { hash: file.hash, dataId };
        result.added++;
        logger.info?.(`cognee-openclaw: remembered ${file.path}${dataId ? ` (dataId=${dataId})` : ""}`);
      }
      syncIndex.datasetId = datasetId;
      syncIndex.datasetName = dsName;
    } catch (error) {
      // One failed batch fails every queued file — surface them all so the
      // caller's error count reflects the actual workload, not just one entry.
      for (const file of toAdd) {
        result.errors++;
        logger.warn?.(`cognee-openclaw: failed to sync ${file.path}: ${error instanceof Error ? error.message : String(error)}`);
      }
    }
  }

  // Per-item /forget for files that disappeared from the workspace.
  // We pass `dsName` (not the UUID) — see CogneeHttpClient.forget().
  const currentPaths = new Set(fullFiles.map(f => f.path));
  for (const [path, entry] of Object.entries(syncIndex.entries)) {
    if (!currentPaths.has(path) && entry.dataId && datasetId) {
      const forgetResult = await client.forget({ dataId: entry.dataId, dataset: dsName });
      if (forgetResult.deleted) {
        result.deleted++;
        delete syncIndex.entries[path];
        logger.info?.(`cognee-openclaw: forgot ${path}`);
      } else {
        const isNotFound = forgetResult.error && (
          forgetResult.error.includes("404") || forgetResult.error.includes("409") || forgetResult.error.includes("not found")
        );
        if (isNotFound) {
          result.deleted++;
          delete syncIndex.entries[path];
          logger.info?.(`cognee-openclaw: forgot ${path} (already removed from Cognee)`);
        } else {
          result.errors++;
          logger.warn?.(`cognee-openclaw: failed to forget ${path}${forgetResult.error ? `: ${forgetResult.error}` : ""}`);
        }
      }
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

    const result = await syncFiles(client, scopeChanged, scopeFull, scopeIndex, cfg, logger, dsName);
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
// Helpers
// ---------------------------------------------------------------------------

function wrapWithMetadata(file: MemoryFile): string {
  return `# ${file.path}\n\n${file.content}\n\n---\nMetadata: ${JSON.stringify({ path: file.path, source: "memory" })}`;
}

// Kept for backwards compatibility with downstream consumers that imported
// these from sync.ts to drive cognify polling. The new /remember path runs
// cognify+improve server-side, so the openclaw sync flow no longer polls.
export let COGNIFY_POLL_INTERVAL_MS = 5_000;
export function _setPollInterval(ms: number): void { COGNIFY_POLL_INTERVAL_MS = ms; }
