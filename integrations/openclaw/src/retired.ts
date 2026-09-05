// ---------------------------------------------------------------------------
// Retired-session bookkeeping shared by the dataset-switch store and the
// persistence loader. Lives in its own module so the loader can prune on read
// and so test suites that mock persistence.ts keep the real pruning logic.
// ---------------------------------------------------------------------------

import type { RetiredSession } from "./types.js";

/** Synced retired sessions kept for the record; unsynced ones are never pruned. */
export const MAX_SYNCED_RETIRED = 20;
/** Previous-dataset trail kept on the override (informational). */
export const MAX_PREVIOUS_DATASETS = 50;

/**
 * Bound a retired list: every unsynced session stays (each is a pending
 * sync), only the oldest *synced* ones beyond MAX_SYNCED_RETIRED are dropped.
 * Order is preserved (oldest first). Applied on every write AND on load, so a
 * lowered cap takes effect without waiting for the next switch.
 */
export function pruneRetired(retired: readonly RetiredSession[]): RetiredSession[] {
  const syncedCount = retired.filter((r) => r.synced).length;
  let toDrop = Math.max(0, syncedCount - MAX_SYNCED_RETIRED);
  const out: RetiredSession[] = [];
  for (const r of retired) {
    if (r.synced && toDrop > 0) { toDrop--; continue; }
    out.push(r);
  }
  return out;
}

