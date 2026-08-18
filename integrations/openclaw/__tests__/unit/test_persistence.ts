/**
 * `src/persistence.ts` against a real filesystem under a temporary HOME.
 *
 * This module owns the sync state that decides what gets re-uploaded: if a load
 * silently returns empty, the next sync re-ingests every file and pays for a
 * cognify it did not need; if a save is lost, the same happens on every run.
 *
 * Driven over a real temp directory rather than a mocked `fs`. The existing
 * `test_syncFiles.ts` mocks `node:fs/promises` and matches on the real
 * `homedir()` paths, which works but proves only that the right path string was
 * passed. Here the round trip is genuine — written bytes are read back.
 *
 * The module resolves its paths at IMPORT time:
 *
 *     export const STATE_DIR = join(homedir(), ".openclaw", "memory", "cognee");
 *
 * so redirecting HOME only works if the module is imported *afterwards*. Hence
 * `jest.resetModules()` plus a fresh `require` in `freshPersistence()` — the
 * TypeScript equivalent of dropping an entry from Python's `sys.modules`.
 *
 * Every test asserts the real home was left alone, because the failure mode this
 * guards is a test quietly editing the developer's own memory files.
 */

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

import {
  expectRealHomeUntouched,
  makeTempDir,
  snapshotRealHome,
  type TempHome,
} from "../../test-utils/tempHome";

type Persistence = typeof import("../../src/persistence");

/**
 * Read lazily by the os mock below, so each test gets its own home.
 *
 * `var`, not `let`: `jest.mock` is hoisted above every declaration and its factory
 * runs during module load, so a `let` is still in its temporal dead zone and the
 * suite fails to start with "Cannot access 'tempHomePath' before initialization".
 */
// eslint-disable-next-line no-var
var tempHomePath = "";

/*
 * Setting process.env.HOME is NOT sufficient. Jest hands each test file its own
 * `process.env`, and mutating it never reaches the C-level getenv that libuv's
 * uv_os_homedir reads — so `os.homedir()` keeps returning the developer's real
 * home. This suite found that out by overwriting a real ~/.openclaw/memory/cognee.
 *
 * The factory is inlined rather than imported from test-utils/tempHome: that module
 * imports `node:os` itself, so requiring it from inside this factory is a circular
 * require that resolves to a half-initialised module.
 */
jest.mock("node:os", () => {
  const actual = jest.requireActual<typeof import("node:os")>("node:os");
  return { ...actual, homedir: () => tempHomePath || actual.homedir() };
});

let home: TempHome;
let realHomeBefore: Map<string, number>;

/** Import persistence fresh so its module-level paths resolve under the temp HOME. */
function freshPersistence(): Persistence {
  let mod: Persistence;
  jest.isolateModules(() => {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    mod = require("../../src/persistence") as Persistence;
  });
  return mod!;
}

/** The state directory inside the temp HOME. */
function stateDir(): string {
  return join(home.path, ".openclaw", "memory", "cognee");
}

/** Seed one of the state files before the module reads it. */
async function seed(fileName: string, body: unknown): Promise<void> {
  await mkdir(stateDir(), { recursive: true });
  await writeFile(
    join(stateDir(), fileName),
    typeof body === "string" ? body : JSON.stringify(body),
    "utf-8",
  );
}

beforeEach(async () => {
  realHomeBefore = await snapshotRealHome();
  home = await makeTempDir();
  tempHomePath = home.path;
});

afterEach(async () => {
  tempHomePath = "";
  await home.cleanup();
  // Every test asserts this: the guard is what caught these very tests writing to
  // the real home when the redirect was only an env var.
  await expectRealHomeUntouched(realHomeBefore);
});

describe("path resolution", () => {
  it("resolves every state path under the redirected HOME", () => {
    // If this fails the whole file is testing the developer's real state, so it is
    // the first thing asserted.
    const p = freshPersistence();
    for (const path of [
      p.STATE_DIR,
      p.STATE_PATH,
      p.SYNC_INDEX_PATH,
      p.SCOPED_SYNC_INDEX_PATH,
      p.AGENT_SYNC_INDEX_PATH,
    ]) {
      expect(path.startsWith(home.path)).toBe(true);
    }
  });
});

describe("dataset state", () => {
  it("is empty when the file does not exist", async () => {
    // A fresh install must not throw here; an ENOENT is the normal first read.
    await expect(freshPersistence().loadDatasetState()).resolves.toEqual({});
  });

  it("round-trips through a real file", async () => {
    const p = freshPersistence();
    await p.saveDatasetState({ testds: "ds-1", other: "ds-2" });

    await expect(p.loadDatasetState()).resolves.toEqual({ testds: "ds-1", other: "ds-2" });
    // Written where the plugin says it writes, and human-readable.
    const raw = await readFile(join(stateDir(), "datasets.json"), "utf-8");
    expect(JSON.parse(raw)).toEqual({ testds: "ds-1", other: "ds-2" });
    expect(raw).toContain("\n");
  });

  it("creates the state directory on first save", async () => {
    const p = freshPersistence();
    await expect(p.saveDatasetState({ a: "1" })).resolves.toBeUndefined();
    await expect(readFile(join(stateDir(), "datasets.json"), "utf-8")).resolves.toContain("a");
  });

  it("treats a non-object payload as empty rather than returning it", async () => {
    // A truncated or hand-edited file must degrade to "no state", not hand a string
    // back to callers that will index into it.
    await seed("datasets.json", JSON.stringify("not an object"));
    await expect(freshPersistence().loadDatasetState()).resolves.toEqual({});
  });
});

describe("legacy sync index", () => {
  it("defaults to an empty entries map when absent", async () => {
    await expect(freshPersistence().loadSyncIndex()).resolves.toEqual({ entries: {} });
  });

  it("backfills a missing entries key", async () => {
    // Callers index `index.entries[path]` unguarded, so a file lacking the key
    // would throw on the next sync.
    await seed("sync-index.json", { datasetId: "ds-1" });

    const loaded = await freshPersistence().loadSyncIndex();
    expect(loaded.entries).toEqual({});
    expect(loaded.datasetId).toBe("ds-1");
  });

  it("round-trips entries", async () => {
    const p = freshPersistence();
    await p.saveSyncIndex({ datasetId: "ds-1", entries: { "MEMORY.md": { hash: "h1" } } } as never);
    await expect(p.loadSyncIndex()).resolves.toMatchObject({
      datasetId: "ds-1",
      entries: { "MEMORY.md": { hash: "h1" } },
    });
  });
});

describe("scoped sync indexes", () => {
  it("is empty when absent", async () => {
    await expect(freshPersistence().loadScopedSyncIndexes()).resolves.toEqual({});
  });

  it("keeps the three valid scopes and discards anything else", async () => {
    // Guards against a typo'd or hand-edited key ("compnay") being loaded as a
    // scope and then written back, entrenching the mistake.
    await seed("scoped-sync-indexes.json", {
      company: { entries: { "a.md": { hash: "h" } } },
      user: { entries: {} },
      agent: { entries: {} },
      compnay: { entries: { "typo.md": { hash: "h" } } },
      nonsense: { entries: {} },
    });

    const loaded = await freshPersistence().loadScopedSyncIndexes();
    expect(Object.keys(loaded).sort()).toEqual(["agent", "company", "user"]);
    expect(loaded.company?.entries).toEqual({ "a.md": { hash: "h" } });
  });

  it("backfills entries per scope", async () => {
    await seed("scoped-sync-indexes.json", { company: { datasetId: "ds-c" } });
    const loaded = await freshPersistence().loadScopedSyncIndexes();
    expect(loaded.company?.entries).toEqual({});
  });

  it("ignores a scope whose value is not an object", async () => {
    await seed("scoped-sync-indexes.json", { company: "nope", user: { entries: {} } });
    const loaded = await freshPersistence().loadScopedSyncIndexes();
    expect(Object.keys(loaded)).toEqual(["user"]);
  });

  it("round-trips through a real file", async () => {
    const p = freshPersistence();
    await p.saveScopedSyncIndexes({ user: { entries: { "u.md": { hash: "h" } } } } as never);
    await expect(p.loadScopedSyncIndexes()).resolves.toMatchObject({
      user: { entries: { "u.md": { hash: "h" } } },
    });
  });
});

describe("migrateLegacyIndex", () => {
  it("returns null when there is nothing to migrate", async () => {
    // Null means "no migration happened", which is what stops the caller from
    // overwriting existing multi-scope state with an empty index.
    await expect(freshPersistence().migrateLegacyIndex("company")).resolves.toBeNull();
  });

  it("moves legacy entries into the requested scope and persists them", async () => {
    await seed("sync-index.json", {
      datasetId: "ds-legacy",
      entries: { "MEMORY.md": { hash: "h1" }, "memory/tools.md": { hash: "h2" } },
    });

    const p = freshPersistence();
    const migrated = await p.migrateLegacyIndex("user");

    expect(Object.keys(migrated ?? {})).toEqual(["user"]);
    expect(migrated?.user?.entries).toEqual({
      "MEMORY.md": { hash: "h1" },
      "memory/tools.md": { hash: "h2" },
    });
    // Persisted, not just returned — otherwise the migration would re-run forever.
    await expect(p.loadScopedSyncIndexes()).resolves.toMatchObject({
      user: { entries: { "MEMORY.md": { hash: "h1" } } },
    });
  });

  it("leaves the legacy file in place", async () => {
    // Documented as harmless-but-kept; deleting it would make a downgrade lose
    // state, and the migration is idempotent because the scoped file wins.
    await seed("sync-index.json", { entries: { "MEMORY.md": { hash: "h1" } } });
    await freshPersistence().migrateLegacyIndex("company");
    await expect(readFile(join(stateDir(), "sync-index.json"), "utf-8")).resolves.toContain("MEMORY.md");
  });
});

describe("per-agent sync indexes", () => {
  it("is empty when absent and round-trips when written", async () => {
    const p = freshPersistence();
    await expect(p.loadAgentSyncIndexes()).resolves.toEqual({});

    await p.saveAgentSyncIndexes({ will: { entries: { "a.md": { hash: "h" } } } } as never);
    await expect(p.loadAgentSyncIndexes()).resolves.toMatchObject({
      will: { entries: { "a.md": { hash: "h" } } },
    });
  });
});
