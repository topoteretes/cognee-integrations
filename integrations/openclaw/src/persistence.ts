import { promises as fs } from "node:fs";
import { dirname, join } from "node:path";
import { homedir } from "node:os";
import type { DatasetState, MemoryScope, ScopedSyncIndexes, SyncIndex } from "./types.js";

// ---------------------------------------------------------------------------
// State file paths
// ---------------------------------------------------------------------------

export const STATE_DIR = join(homedir(), ".openclaw", "memory", "cognee");
export const STATE_PATH = join(STATE_DIR, "datasets.json");
export const SYNC_INDEX_PATH = join(STATE_DIR, "sync-index.json");
export const SCOPED_SYNC_INDEX_PATH = join(STATE_DIR, "scoped-sync-indexes.json");

// ---------------------------------------------------------------------------
// Dataset state (maps dataset name -> dataset ID)
// ---------------------------------------------------------------------------

export async function loadDatasetState(): Promise<DatasetState> {
  try {
    const raw = await fs.readFile(STATE_PATH, "utf-8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    return parsed as DatasetState;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
}

export async function saveDatasetState(state: DatasetState): Promise<void> {
  await fs.mkdir(dirname(STATE_PATH), { recursive: true });
  await fs.writeFile(STATE_PATH, JSON.stringify(state, null, 2), "utf-8");
}

// ---------------------------------------------------------------------------
// Sync index (legacy single-scope)
// ---------------------------------------------------------------------------

export async function loadSyncIndex(): Promise<SyncIndex> {
  try {
    const raw = await fs.readFile(SYNC_INDEX_PATH, "utf-8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return { entries: {} };
    const record = parsed as SyncIndex;
    record.entries ??= {};
    return record;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return { entries: {} };
    throw error;
  }
}

export async function saveSyncIndex(state: SyncIndex): Promise<void> {
  await fs.mkdir(dirname(SYNC_INDEX_PATH), { recursive: true });
  await fs.writeFile(SYNC_INDEX_PATH, JSON.stringify(state, null, 2), "utf-8");
}

// ---------------------------------------------------------------------------
// Scoped sync indexes (multi-scope)
//
// Fix #6: On load, we validate that keys are valid MemoryScope values
// and discard any garbage entries (e.g. typos like "compnay").
//
// Fix #7: Migration support — when switching from single to multi-scope,
// we migrate the legacy sync index into the appropriate scope.
// ---------------------------------------------------------------------------

const VALID_SCOPES = new Set<string>(["company", "user", "agent"]);

export async function loadScopedSyncIndexes(): Promise<ScopedSyncIndexes> {
  try {
    const raw = await fs.readFile(SCOPED_SYNC_INDEX_PATH, "utf-8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    // Validate keys are valid scopes
    const result: ScopedSyncIndexes = {};
    for (const [key, value] of Object.entries(parsed)) {
      if (VALID_SCOPES.has(key) && value && typeof value === "object") {
        const idx = value as SyncIndex;
        idx.entries ??= {};
        result[key as MemoryScope] = idx;
      }
    }
    return result;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
}

export async function saveScopedSyncIndexes(indexes: ScopedSyncIndexes): Promise<void> {
  await fs.mkdir(dirname(SCOPED_SYNC_INDEX_PATH), { recursive: true });
  await fs.writeFile(SCOPED_SYNC_INDEX_PATH, JSON.stringify(indexes, null, 2), "utf-8");
}

/**
 * Fix #7: Migrate legacy single-scope sync index into multi-scope indexes.
 * Moves all entries from the old sync-index.json into the specified default scope.
 * After migration, the legacy file is left in place (harmless) but no longer read.
 */
export async function migrateLegacyIndex(defaultScope: MemoryScope): Promise<ScopedSyncIndexes | null> {
  const legacy = await loadSyncIndex();
  if (Object.keys(legacy.entries).length === 0) return null;

  const scoped: ScopedSyncIndexes = {
    [defaultScope]: { ...legacy },
  };
  await saveScopedSyncIndexes(scoped);
  return scoped;
}
