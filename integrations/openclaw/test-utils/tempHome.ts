/**
 * Temporary HOME isolation, and a guard proving the real one stayed untouched.
 *
 * The plugin reads `homedir()` for everything persistent: `~/.cognee-plugin`
 * (shared with the claude-code and codex plugins) and
 * `~/.openclaw/memory/cognee/{sync-index,datasets,scoped-sync-indexes}.json`.
 *
 * Today the suite avoids writing there by mocking `node:fs/promises` and using the
 * real `homedir()` paths only as expected values. That works, but it is one
 * forgotten mock away from a test that edits the developer's own memory files —
 * and the failure would look like flakiness, not like damage. `expectRealHomeUntouched`
 * turns that into an assertion.
 *
 * `src/server.ts` resolves `~/.cognee-plugin` at MODULE LOAD, not per call:
 *
 *     const COGNEE_PLUGIN_BASE = join(homedir(), ".cognee-plugin");
 *
 * so setting HOME after that module is imported changes nothing. Tests that need
 * a redirected home must set it and then re-import — `jest.resetModules()` plus a
 * fresh `require`/dynamic `import`, which is this stack's equivalent of dropping
 * an entry from Python's `sys.modules`.
 */

import { mkdtemp, rm, stat } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";

/** Files under the real home that no test may create or modify. */
export const REAL_HOME_PATHS = [
  join(homedir(), ".openclaw", "memory", "cognee", "sync-index.json"),
  join(homedir(), ".openclaw", "memory", "cognee", "datasets.json"),
  join(homedir(), ".openclaw", "memory", "cognee", "scoped-sync-indexes.json"),
  join(homedir(), ".cognee-plugin", "api_key.json"),
  join(homedir(), ".cognee-plugin", "ensure_and_boot.py"),
];

export interface TempHome {
  path: string;
  /** Restore the previous HOME/USERPROFILE and delete the directory. */
  cleanup: () => Promise<void>;
}

/**
 * Point HOME (and USERPROFILE, which is what `homedir()` reads on Windows) at a
 * fresh directory.
 *
 * Call this BEFORE importing any module that resolves `homedir()` at load time,
 * or the redirect is silently ignored — see the note above about `src/server.ts`.
 */
export async function useTempHome(): Promise<TempHome> {
  const previous = { HOME: process.env.HOME, USERPROFILE: process.env.USERPROFILE };
  const path = await mkdtemp(join(tmpdir(), "cognee-openclaw-home-"));
  process.env.HOME = path;
  process.env.USERPROFILE = path;

  return {
    path,
    cleanup: async () => {
      if (previous.HOME === undefined) delete process.env.HOME;
      else process.env.HOME = previous.HOME;
      if (previous.USERPROFILE === undefined) delete process.env.USERPROFILE;
      else process.env.USERPROFILE = previous.USERPROFILE;
      await rm(path, { recursive: true, force: true });
    },
  };
}

/** `{path: mtimeMs}` for the real-home files that exist right now. */
export async function snapshotRealHome(): Promise<Map<string, number>> {
  const snapshot = new Map<string, number>();
  for (const path of REAL_HOME_PATHS) {
    try {
      snapshot.set(path, (await stat(path)).mtimeMs);
    } catch {
      // Absent is the normal case on a clean machine; recorded by omission so a
      // file appearing during the test is caught below.
    }
  }
  return snapshot;
}

/**
 * Assert no real-home file was created or modified since the snapshot.
 *
 * Compares mtimes rather than contents: a test that rewrites a file with the same
 * bytes has still escaped its sandbox, and that is the thing worth catching.
 */
export async function expectRealHomeUntouched(before: Map<string, number>): Promise<void> {
  const damaged: string[] = [];
  for (const path of REAL_HOME_PATHS) {
    let mtime: number | undefined;
    try {
      mtime = (await stat(path)).mtimeMs;
    } catch {
      mtime = undefined;
    }
    const previous = before.get(path);
    if (mtime === undefined && previous === undefined) continue;
    if (mtime === undefined) damaged.push(`${path} (deleted)`);
    else if (previous === undefined) damaged.push(`${path} (created)`);
    else if (mtime !== previous) damaged.push(`${path} (modified)`);
  }
  if (damaged.length) {
    throw new Error(
      `a test wrote to the REAL home — check for a missing fs mock or an unset temp HOME:\n  ${damaged.join("\n  ")}`,
    );
  }
}
