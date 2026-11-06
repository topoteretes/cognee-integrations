// ---------------------------------------------------------------------------
// One-time "recall stays pinned" notice (SDK-1393)
// ---------------------------------------------------------------------------
//
// A configured non-default `datasetName` with no recall mode set keeps recall
// pinned to that dataset. Those users are told once, per dataset, that auto
// recall exists. The marker lives next to the plugin's other state files so
// the notice survives gateway restarts.
// ---------------------------------------------------------------------------

import { promises as fs } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

/** dataset name -> ISO time the notice was shown. */
export type RecallNoticesFile = Record<string, string>;

/** Resolved at call time (not module load) so the suite-wide homedir sandbox applies. */
export function recallNoticesPath(): string {
  return join(homedir(), ".openclaw", "memory", "cognee", "recall-notices.json");
}

export function recallPinNotice(dataset: string): string {
  return (
    `Cognee: recall stays on \`${dataset}\`. Cognee can also pick the best-matching dataset for each prompt ` +
    `from all you can read: set \`recallMode: "auto"\` in the plugin config, or ask to set recall to auto for this conversation.`
  );
}

export async function loadRecallNotices(path = recallNoticesPath()): Promise<RecallNoticesFile> {
  try {
    const parsed = JSON.parse(await fs.readFile(path, "utf-8"));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: RecallNoticesFile = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === "string") out[k] = v;
    }
    return out;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
}

/** Record that the notice for `dataset` was shown (read-modify-write, so other datasets' markers stay). */
export async function markRecallNoticeShown(dataset: string, path = recallNoticesPath(), now = new Date()): Promise<void> {
  const current = await loadRecallNotices(path).catch(() => ({} as RecallNoticesFile));
  current[dataset] = now.toISOString();
  await fs.mkdir(dirname(path), { recursive: true });
  await fs.writeFile(path, JSON.stringify(current, null, 2), "utf-8");
}
