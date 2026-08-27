/**
 * Dataset switching primitives: the override store (keys, persistence,
 * minted session suffixes) and the memory_switch_dataset tool with fake
 * server calls — strict sync before switching, force override, validation,
 * already-active, reset.
 */

import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { resolveConfig } from "../../src/config";
import {
  DatasetSwitchStore,
  conversationKeys,
  createDatasetSwitchTool,
  isValidDatasetName,
  loadDatasetOverrides,
  withSessionSuffix,
  type DatasetSwitchDeps,
  type MemorySwitchDatasetResult,
} from "../../src/dataset-switch";

const cfg = resolveConfig({ datasetName: "testds" });

describe("helpers", () => {
  it("conversationKeys prefers sessionKey and falls back to sessionId", () => {
    expect(conversationKeys({ sessionKey: "agent:will:main", sessionId: "s1" })).toEqual(["key:agent:will:main", "sid:s1"]);
    expect(conversationKeys({ sessionId: "s1" })).toEqual(["sid:s1"]);
    expect(conversationKeys(undefined)).toEqual([]);
  });

  it("validates dataset names", () => {
    expect(isValidDatasetName("project-x_2.0")).toBe(true);
    expect(isValidDatasetName("")).toBe(false);
    expect(isValidDatasetName("-leading")).toBe(false);
    expect(isValidDatasetName("has space")).toBe(false);
    expect(isValidDatasetName("a".repeat(129))).toBe(false);
  });

  it("withSessionSuffix appends the ordinal only when switched", () => {
    expect(withSessionSuffix("open_claw_s1", undefined)).toBe("open_claw_s1");
    expect(withSessionSuffix("open_claw_s1", { dataset: "x", sessionSuffix: 3, switchedAt: "", previous: [], retired: [] })).toBe("open_claw_s1__3");
    expect(withSessionSuffix("", { dataset: "x", sessionSuffix: 2, switchedAt: "", previous: [], retired: [] })).toBe("");
  });
});

describe("DatasetSwitchStore", () => {
  let dir: string;
  let path: string;
  beforeEach(async () => {
    dir = await mkdtemp(join(tmpdir(), "cognee-switch-"));
    path = join(dir, "overrides.json");
  });
  afterEach(async () => rm(dir, { recursive: true, force: true }));

  it("records a switch under both keys, mints increasing suffixes, and persists", async () => {
    const now = new Date("2026-08-27T10:00:00Z");
    const store = new DatasetSwitchStore({ path, now: () => now });
    await store.ready();
    const first = store.set({ sessionKey: "k1", sessionId: "s1" }, "proj-a", "testds");
    expect(first).toEqual({ dataset: "proj-a", sessionSuffix: 2, switchedAt: "2026-08-27T10:00:00.000Z", previous: ["testds"], retired: [] });
    expect(store.get({ sessionKey: "k1" })?.dataset).toBe("proj-a");
    expect(store.get({ sessionId: "s1" })?.dataset).toBe("proj-a");
    expect(store.get({ sessionId: "other" })).toBeUndefined();

    const second = store.set({ sessionKey: "k1", sessionId: "s1" }, "proj-b", "proj-a");
    expect(second.sessionSuffix).toBe(3);
    expect(second.previous).toEqual(["testds", "proj-a"]);
    await store.flush();

    const onDisk = JSON.parse(await readFile(path, "utf-8"));
    expect(Object.keys(onDisk).sort()).toEqual(["key:k1", "sid:s1"]);

    const reloaded = new DatasetSwitchStore({ path });
    await reloaded.ready();
    expect(reloaded.get({ sessionKey: "k1" })).toEqual(second);
  });

  it("clear removes every alias and reports whether anything was removed", async () => {
    const store = new DatasetSwitchStore({ path });
    await store.ready();
    store.set({ sessionKey: "k1", sessionId: "s1" }, "proj-a", "testds");
    expect(store.clear({ sessionId: "s1" })).toBe(true);
    expect(store.get({ sessionKey: "k1" })).toBeUndefined();
    expect(store.clear({ sessionKey: "k1" })).toBe(false);
    await store.flush();
    expect(await loadDatasetOverrides(path)).toEqual({});
  });

  it("drops malformed rows on load and tolerates a missing file", async () => {
    await rm(path, { force: true });
    expect(await loadDatasetOverrides(path)).toEqual({});
    const { writeFile } = await import("node:fs/promises");
    await writeFile(path, JSON.stringify({ "key:a": { dataset: "x" }, "key:b": { nope: 1 }, "key:c": "str" }));
    expect(await loadDatasetOverrides(path)).toEqual({ "key:a": { dataset: "x", sessionSuffix: 2, switchedAt: "", previous: [], retired: [] } });
  });

  it("tracks retired sessions across switches and flips synced only on request", async () => {
    const store = new DatasetSwitchStore({ path, now: () => new Date("2026-08-27T10:00:00Z") });
    await store.ready();
    const ctx = { sessionKey: "k1", sessionId: "s1" };
    store.set(ctx, "proj-a", "testds", { sessionId: "open_claw_s1", synced: false });
    store.set(ctx, "proj-b", "proj-a", { sessionId: "open_claw_s1__2", synced: true });
    expect(store.get(ctx)?.retired).toEqual([
      { dataset: "testds", sessionId: "open_claw_s1", synced: false, retiredAt: "2026-08-27T10:00:00.000Z" },
      { dataset: "proj-a", sessionId: "open_claw_s1__2", synced: true, retiredAt: "2026-08-27T10:00:00.000Z" },
    ]);
    expect(store.unsyncedRetired(ctx).map((r) => r.sessionId)).toEqual(["open_claw_s1"]);
    store.markRetiredSynced(ctx, "open_claw_s1");
    expect(store.unsyncedRetired(ctx)).toEqual([]);
    await store.flush();
    expect((await loadDatasetOverrides(path))["key:k1"].retired.every((r) => r.synced)).toBe(true);
  });
});

describe("memory_switch_dataset tool", () => {
  let dir: string;
  let store: DatasetSwitchStore;
  const ctx = { agentId: "will", sessionId: "s1", sessionKey: "agent:will:main" };

  function deps(overrides: Partial<DatasetSwitchDeps> = {}): DatasetSwitchDeps & { synced: Array<[string, string]>; ensured: string[]; remembered: Array<[string, string]> } {
    const synced: Array<[string, string]> = [];
    const ensured: string[] = [];
    const remembered: Array<[string, string]> = [];
    return {
      cfg,
      store,
      currentDataset: (c) => store.get(c)?.dataset ?? cfg.datasetName,
      currentSessionId: (c) => (c.sessionId ? withSessionSuffix(`open_claw_${c.sessionId}`, store.get(c)) : undefined),
      listDatasets: async () => [{ id: "ds-1", name: "testds" }, { id: "ds-2", name: "proj-a" }],
      ensureDataset: async (name) => { ensured.push(name); return `id-${name}`; },
      syncSession: async (ds, sid) => { synced.push([ds, sid]); },
      rememberDatasetId: async (name, id) => { remembered.push([name, id]); },
      synced, ensured, remembered,
      ...overrides,
    };
  }

  async function run(d: DatasetSwitchDeps, params: Record<string, unknown>, c: Record<string, unknown> = ctx) {
    const tool = createDatasetSwitchTool(d, c);
    return (await tool.execute("call", params as never)).details as MemorySwitchDatasetResult;
  }

  beforeEach(async () => {
    dir = await mkdtemp(join(tmpdir(), "cognee-switch-tool-"));
    store = new DatasetSwitchStore({ path: join(dir, "o.json") });
    await store.ready();
  });
  afterEach(async () => {
    // Let the store's coalesced save land before the directory disappears.
    await store.flush();
    await rm(dir, { recursive: true, force: true });
  });

  it("lists datasets with the current one first and flagged", async () => {
    const out = await run(deps(), { action: "list" });
    expect(out).toMatchObject({ action: "list", current: "testds" });
    expect((out as { datasets: Array<{ name: string; current: boolean }> }).datasets).toEqual([
      { name: "testds", id: "ds-1", current: true },
      { name: "proj-a", id: "ds-2", current: false },
    ]);
  });

  it("switches: syncs the current session strictly, ensures the target, records the override with a fresh session id", async () => {
    const d = deps();
    const out = await run(d, { action: "switch", dataset: "proj-a" });
    expect(out).toMatchObject({
      action: "switch",
      switched: true,
      dataset: "proj-a",
      sessionId: "open_claw_s1__2",
      previous: { dataset: "testds", sessionId: "open_claw_s1", synced: true },
    });
    expect(d.synced).toEqual([["testds", "open_claw_s1"]]);
    expect(d.ensured).toEqual(["proj-a"]);
    expect(d.remembered).toEqual([["proj-a", "id-proj-a"]]);
    expect(store.get(ctx)?.dataset).toBe("proj-a");

    const cur = await run(d, { action: "current" });
    expect(cur).toMatchObject({ action: "current", dataset: "proj-a", sessionId: "open_claw_s1__2", switched: true, previous: ["testds"] });
  });

  it("aborts and changes nothing when the sync fails, unless force=true", async () => {
    const d = deps({ syncSession: async () => { throw new Error("improve 503"); } });
    const out = await run(d, { action: "switch", dataset: "proj-a" });
    expect(out).toMatchObject({ action: "switch", switched: false, dataset: "testds" });
    expect((out as { error: string }).error).toMatch(/syncing the current session into "testds" failed.*Nothing was changed/);
    expect(d.ensured).toEqual([]);
    expect(store.get(ctx)).toBeUndefined();

    const forced = await run(d, { action: "switch", dataset: "proj-a", force: true });
    expect(forced).toMatchObject({ switched: true, dataset: "proj-a", previous: { dataset: "testds", synced: false } });
    expect((forced as { note: string }).note).toMatch(/NOT synced \(force\); it is re-synced automatically at session end/);
    // The unsynced session is remembered so session end / reset can bridge it.
    expect(store.unsyncedRetired(ctx)).toEqual([expect.objectContaining({ dataset: "testds", sessionId: "open_claw_s1", synced: false })]);
  });

  it("reset re-syncs unsynced retired sessions first, and refuses without force when that fails", async () => {
    let failSync = true;
    const synced: Array<[string, string]> = [];
    const d = deps({ syncSession: async (ds, sid) => { if (failSync) throw new Error("improve 503"); synced.push([ds, sid]); } });
    await run(d, { action: "switch", dataset: "proj-a", force: true });

    const refused = await run(d, { action: "reset" });
    expect(refused).toMatchObject({ action: "reset", reset: false, dataset: "proj-a", unsynced: ["open_claw_s1"] });
    expect((refused as { error: string }).error).toMatch(/could not be synced.*nothing was changed/i);
    expect(store.get(ctx)?.dataset).toBe("proj-a");

    failSync = false;
    const ok = await run(d, { action: "reset" });
    expect(ok).toEqual({ action: "reset", reset: true, dataset: "testds", resynced: ["open_claw_s1"] });
    expect(synced).toEqual([["testds", "open_claw_s1"]]);
    expect(store.get(ctx)).toBeUndefined();
  });

  it("reset with force drops the override even when a retired session stays unsynced, and says so", async () => {
    const d = deps({ syncSession: async () => { throw new Error("down"); } });
    await run(d, { action: "switch", dataset: "proj-a", force: true });
    const out = await run(d, { action: "reset", force: true });
    expect(out).toMatchObject({ reset: true, dataset: "testds", unsynced: ["open_claw_s1"] });
    expect((out as { note: string }).note).toMatch(/not bridged/);
    expect(store.get(ctx)).toBeUndefined();
  });

  it("rejects bad input and reports already_active without side effects", async () => {
    const d = deps();
    expect((await run(d, { action: "switch" })) as { error: string }).toMatchObject({ switched: false, error: "dataset is required" });
    expect(((await run(d, { action: "switch", dataset: "bad name!" })) as { error: string }).error).toMatch(/invalid dataset name/);
    expect(await run(d, { action: "switch", dataset: "testds" })).toMatchObject({ switched: false, reason: "already_active" });
    expect(((await run(d, { action: "switch", dataset: "proj-a" }, { agentId: "will" })) as { error: string }).error).toMatch(/no conversation\/session/);
    expect(d.synced).toEqual([]);
    expect(d.ensured).toEqual([]);
  });

  it("surfaces a failed ensureDataset without recording the override", async () => {
    const d = deps({ ensureDataset: async () => { throw new Error("403"); } });
    const out = await run(d, { action: "switch", dataset: "proj-a" });
    expect((out as { error: string }).error).toMatch(/could not create\/resolve dataset "proj-a"/);
    expect(store.get(ctx)).toBeUndefined();
  });

  it("reset returns to the configured dataset", async () => {
    const d = deps();
    await run(d, { action: "switch", dataset: "proj-a" });
    expect(await run(d, { action: "reset" })).toEqual({ action: "reset", reset: true, dataset: "testds" });
    expect(await run(d, { action: "reset" })).toMatchObject({ reset: false, dataset: "testds" });
  });
});
