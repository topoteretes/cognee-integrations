/**
 * Dataset switching primitives: the override store (keys, persistence,
 * minted session suffixes) and the memory_switch_dataset tool with fake
 * server calls — strict sync before switching, force override, validation,
 * already-active, reset.
 */

import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { datasetPinsRecall, resolveConfig } from "../../src/config";
import {
  DatasetSwitchStore,
  MAX_SYNCED_RETIRED,
  configPinsRecall,
  conversationKeys,
  createDatasetSwitchTool,
  isValidDatasetName,
  loadDatasetOverrides,
  pruneRetired,
  withSessionSuffix,
  type DatasetOverride,
  type DatasetSwitchDeps,
  type MemorySwitchDatasetResult,
} from "../../src/dataset-switch";

// The default config: recall is auto unless a conversation pins it (or the
// config pins it: recallMode "pinned", or a non-default datasetName with no
// recall mode — see "configured recallMode").
const cfg = resolveConfig({});

describe("helpers", () => {
  it("conversationKeys prefers sessionKey and falls back to sessionId", () => {
    expect(conversationKeys({ sessionKey: "agent:will:main", sessionId: "s1" })).toEqual(["key:agent:will:main", "sid:s1"]);
    expect(conversationKeys({ sessionId: "s1" })).toEqual(["sid:s1"]);
    expect(conversationKeys(undefined)).toEqual([]);
  });

  it("validates dataset names", () => {
    expect(isValidDatasetName("project-x_2")).toBe(true);
    // cognee rejects dots in dataset names, so the switch must too.
    expect(isValidDatasetName("project-x_2.0")).toBe(false);
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
    expect(first).toEqual({ dataset: "proj-a", sessionSuffix: 2, switchedAt: "2026-08-27T10:00:00.000Z", previous: ["testds"], retired: [], recall: "pinned" });
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
    // A recall-mode choice (no dataset) is kept; a bad mode or an empty dataset is not.
    await writeFile(path, JSON.stringify({ "key:m": { recall: "auto", chosenAt: "t" }, "key:n": { recall: "bogus" }, "key:o": { dataset: "", recall: "auto" } }));
    expect(await loadDatasetOverrides(path)).toEqual({ "key:m": { recall: "auto", chosenAt: "t" } });
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
    expect(((await loadDatasetOverrides(path))["key:k1"] as DatasetOverride).retired.every((r) => r.synced)).toBe(true);
  });

  it("prunes only the oldest synced retired sessions beyond the cap; unsynced ones are never dropped", async () => {
    const store = new DatasetSwitchStore({ path });
    await store.ready();
    const ctx = { sessionKey: "k1", sessionId: "s1" };
    // 25 synced switches, with one unsynced in the middle.
    for (let i = 0; i < 25; i++) {
      store.set(ctx, `ds-${i + 1}`, `ds-${i}`, { sessionId: `sid-${i}`, synced: i !== 3 });
    }
    const retired = store.get(ctx)!.retired;
    expect(retired.filter((r) => r.synced)).toHaveLength(MAX_SYNCED_RETIRED);
    expect(retired.filter((r) => !r.synced).map((r) => r.sessionId)).toEqual(["sid-3"]);
    // Oldest synced entries were the ones dropped; order preserved.
    expect(retired[0].sessionId).toBe("sid-3");
    expect(retired.map((r) => r.sessionId).slice(-2)).toEqual(["sid-23", "sid-24"]);

    expect(pruneRetired([])).toEqual([]);
    await store.flush(); // let the coalesced save land before afterEach removes the dir
  });

  it("applies the cap on load too, so an oversized file shrinks without waiting for a switch", async () => {
    const retired = Array.from({ length: MAX_SYNCED_RETIRED + 5 }, (_, i) => ({ dataset: "d", sessionId: `s-${i}`, synced: i !== 1, retiredAt: "" }));
    const { writeFile } = await import("node:fs/promises");
    await writeFile(path, JSON.stringify({ "key:k": { dataset: "x", sessionSuffix: 2, switchedAt: "", previous: [], retired } }));
    const loaded = ((await loadDatasetOverrides(path))["key:k"] as DatasetOverride).retired;
    expect(loaded.filter((r) => r.synced)).toHaveLength(MAX_SYNCED_RETIRED);
    expect(loaded.some((r) => r.sessionId === "s-1" && !r.synced)).toBe(true);
  });

  it("shared() hands every caller the same instance per file, so duplicate register() cannot clobber writes", async () => {
    const a = DatasetSwitchStore.shared({ path });
    const b = DatasetSwitchStore.shared({ path });
    expect(a).toBe(b);
    expect(DatasetSwitchStore.shared({ path: join(dir, "other.json") })).not.toBe(a);
    await a.ready();
    a.set({ sessionKey: "k1", sessionId: "s1" }, "proj-a", "testds");
    expect(b.get({ sessionKey: "k1" })?.dataset).toBe("proj-a");
    await a.flush();
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
      currentDataset: (c) => store.get(c)?.dataset ?? (overrides.cfg ?? cfg).datasetName,
      currentSessionId: (c) => (c.sessionId ? withSessionSuffix(`open_claw_${c.sessionId}`, store.get(c)) : undefined),
      listDatasets: async () => [{ id: "ds-1", name: "agent_sessions" }, { id: "ds-2", name: "proj-a" }],
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
    expect(out).toMatchObject({ action: "list", current: "agent_sessions" });
    expect((out as { datasets: Array<{ name: string; current: boolean }> }).datasets).toEqual([
      { name: "agent_sessions", id: "ds-1", current: true },
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
      previous: { dataset: "agent_sessions", sessionId: "open_claw_s1", synced: true },
    });
    expect(d.synced).toEqual([["agent_sessions", "open_claw_s1"]]);
    expect(d.ensured).toEqual(["proj-a"]);
    expect(d.remembered).toEqual([["proj-a", "id-proj-a"]]);
    expect(store.get(ctx)?.dataset).toBe("proj-a");

    const cur = await run(d, { action: "current" });
    expect(cur).toMatchObject({ action: "current", dataset: "proj-a", sessionId: "open_claw_s1__2", switched: true, previous: ["agent_sessions"] });
  });

  it("aborts and changes nothing when the sync fails, unless force=true", async () => {
    const d = deps({ syncSession: async () => { throw new Error("improve 503"); } });
    const out = await run(d, { action: "switch", dataset: "proj-a" });
    expect(out).toMatchObject({ action: "switch", switched: false, dataset: "agent_sessions" });
    expect((out as { error: string }).error).toMatch(/syncing the current session into "agent_sessions" failed.*Nothing was changed/);
    expect(d.ensured).toEqual([]);
    expect(store.get(ctx)).toBeUndefined();

    const forced = await run(d, { action: "switch", dataset: "proj-a", force: true });
    expect(forced).toMatchObject({ switched: true, dataset: "proj-a", previous: { dataset: "agent_sessions", synced: false } });
    expect((forced as { note: string }).note).toMatch(/NOT synced \(force\); it is re-synced automatically at session end/);
    // The unsynced session is remembered so session end / reset can bridge it.
    expect(store.unsyncedRetired(ctx)).toEqual([expect.objectContaining({ dataset: "agent_sessions", sessionId: "open_claw_s1", synced: false })]);
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
    expect(ok).toMatchObject({ action: "reset", reset: true, dataset: "agent_sessions", resynced: ["open_claw_s1"], recall: "auto" });
    expect(ok).not.toHaveProperty("unsynced");
    expect(synced).toEqual([["agent_sessions", "open_claw_s1"]]);
    expect(store.get(ctx)).toBeUndefined();
  });

  it("reset with force drops the override even when a retired session stays unsynced, and says so", async () => {
    const d = deps({ syncSession: async () => { throw new Error("down"); } });
    await run(d, { action: "switch", dataset: "proj-a", force: true });
    const out = await run(d, { action: "reset", force: true });
    expect(out).toMatchObject({ reset: true, dataset: "agent_sessions", unsynced: ["open_claw_s1"] });
    expect((out as { note: string }).note).toMatch(/not bridged/);
    expect(store.get(ctx)).toBeUndefined();
  });

  it("rejects bad input, and switching to the dataset in use is the same as pin", async () => {
    const d = deps();
    expect((await run(d, { action: "switch" })) as { error: string }).toMatchObject({ switched: false, error: "dataset is required" });
    expect(((await run(d, { action: "switch", dataset: "bad name!" })) as { error: string }).error).toMatch(/invalid dataset name/);
    const picked = await run(d, { action: "switch", dataset: "agent_sessions" });
    expect(picked).toMatchObject({ action: "switch", switched: false, changed: true, recall: "pinned", reason: "dataset_picked" });
    expect(store.recallPinned(ctx)).toBe(true);
    // A pin is a mode, not a switch: no dataset override, same session id.
    expect(store.get(ctx)).toBeUndefined();
    expect(withSessionSuffix("open_claw_s1", store.get(ctx))).toBe("open_claw_s1");
    expect(await run(d, { action: "switch", dataset: "agent_sessions" })).toMatchObject({ switched: false, changed: false, reason: "dataset_picked" });
    expect(((await run(d, { action: "switch", dataset: "proj-a" }, { agentId: "will" })) as { error: string }).error).toMatch(/no conversation\/session/);
    expect(d.synced).toEqual([]);
    expect(d.ensured).toEqual([]);
  });

  it("reserves \"auto\": a switch to it is an error and changes nothing", async () => {
    const d = deps();
    for (const name of ["auto", " Auto "]) {
      const out = await run(d, { action: "switch", dataset: name });
      expect(out).toMatchObject({ action: "switch", switched: false, dataset: "agent_sessions" });
      expect((out as { error: string }).error).toMatch(/"auto" is reserved.*action="auto"/);
    }
    expect(store.get(ctx)).toBeUndefined();
    expect(d.synced).toEqual([]);
    expect(d.ensured).toEqual([]);
  });


  it("in multi-scope mode reports recall as restricted to the configured scopes", async () => {
    const multi = resolveConfig({ datasetName: "agent_sessions", companyDataset: "acme", recallScopes: ["company", "agent"] });
    const d = deps({ cfg: multi });
    const cur = await run(d, { action: "current" });
    expect(cur).toMatchObject({ recall: "pinned", reason: "multi_scope", scopes: ["company", "agent"] });
    expect((cur as { note: string }).note).toMatch(/configured scopes \(company, agent\)/);
    expect(await run(d, { action: "auto" })).toMatchObject({ changed: false, recall: "pinned", reason: "multi_scope" });
    expect(await run(d, { action: "pin" })).toMatchObject({ changed: false, recall: "pinned", reason: "multi_scope" });
    expect(store.has(ctx)).toBe(false);
  });

  it("recall is auto until a switch, and action=auto lifts the restriction without moving saves (SDK-1393)", async () => {
    const d = deps();
    expect(await run(d, { action: "current" })).toMatchObject({ recall: "auto", switched: false });
    expect(await run(d, { action: "auto" })).toMatchObject({ action: "auto", changed: false, recall: "auto" });

    const sw = await run(d, { action: "switch", dataset: "proj-a" });
    expect(sw).toMatchObject({ switched: true, recall: "pinned", reason: "dataset_picked" });
    expect(store.recallPinned(ctx)).toBe(true);
    expect(await run(d, { action: "current" })).toMatchObject({ dataset: "proj-a", recall: "pinned", reason: "dataset_picked", switched: true });

    expect(await run(d, { action: "auto" })).toMatchObject({ action: "auto", dataset: "proj-a", changed: true, recall: "auto" });
    expect(store.recallPinned(ctx)).toBe(false);
    // Saves still go to the switched dataset.
    expect(store.get(ctx)?.dataset).toBe("proj-a");
    // pin re-pins the switched dataset without another session.
    expect(await run(d, { action: "pin" })).toMatchObject({ action: "pin", dataset: "proj-a", changed: true, recall: "pinned" });
    expect(store.get(ctx)?.sessionSuffix).toBe(2);
  });

  it("pin records a mode only: no dataset override on disk", async () => {
    await run(deps(), { action: "pin" });
    await store.flush();
    const onDisk = JSON.parse(await readFile(join(dir, "o.json"), "utf-8"));
    for (const k of conversationKeys(ctx)) {
      expect(onDisk[k]).toEqual({ recall: "pinned", chosenAt: expect.any(String) });
      expect(onDisk[k]).not.toHaveProperty("dataset");
    }
  });

  it("a real switch after a pin works: new suffixed session, recall pinned to the new dataset", async () => {
    const d = deps();
    await run(d, { action: "pin" });
    const sw = await run(d, { action: "switch", dataset: "proj-a" });
    expect(sw).toMatchObject({ switched: true, dataset: "proj-a", sessionId: "open_claw_s1__2", previous: { dataset: "agent_sessions", sessionId: "open_claw_s1" }, recall: "pinned" });
    expect(store.get(ctx)).toMatchObject({ dataset: "proj-a", sessionSuffix: 2, recall: "pinned" });
    expect(await run(d, { action: "auto" })).toMatchObject({ changed: true, recall: "auto", dataset: "proj-a" });
    expect(store.get(ctx)).toMatchObject({ dataset: "proj-a", sessionSuffix: 2, recall: "auto" });
  });

  it("aliases agree after a reload: change the mode through one key, both keys report it", async () => {
    const path = join(dir, "o.json");
    const both = { sessionKey: ctx.sessionKey, sessionId: ctx.sessionId };
    for (const seed of ["pin", "switch"] as const) {
      store.clear(both);
      if (seed === "pin") await run(deps(), { action: "pin" });
      else await run(deps(), { action: "switch", dataset: "proj-a" });
      await store.flush();

      const reloaded = new DatasetSwitchStore({ path });
      await reloaded.ready();
      // After a reload the two keys hold separate objects; a change must reach both.
      expect(reloaded.setRecall(both, "auto")).toBe(true);
      expect(reloaded.recallMode({ sessionKey: ctx.sessionKey })).toBe("auto");
      expect(reloaded.recallMode({ sessionId: ctx.sessionId })).toBe("auto");
      if (seed === "switch") {
        expect(reloaded.get({ sessionId: ctx.sessionId })).toEqual(reloaded.get({ sessionKey: ctx.sessionKey }));
        // markRetiredSynced through one key also reaches the other.
        reloaded.set(both, "proj-b", "proj-a", { sessionId: "old", synced: false });
        reloaded.markRetiredSynced({ sessionKey: ctx.sessionKey }, "old");
        expect(reloaded.unsyncedRetired({ sessionId: ctx.sessionId })).toEqual([]);
      }
      reloaded.clear({ sessionKey: ctx.sessionKey });
      expect(reloaded.has({ sessionId: ctx.sessionId })).toBe(false);
      await reloaded.flush();
    }
  });

  it("surfaces a failed ensureDataset without recording the override", async () => {
    const d = deps({ ensureDataset: async () => { throw new Error("403"); } });
    const out = await run(d, { action: "switch", dataset: "proj-a" });
    expect((out as { error: string }).error).toMatch(/could not create\/resolve dataset "proj-a"/);
    expect(store.get(ctx)).toBeUndefined();
  });


  describe("configured recallMode", () => {
    const pinnedCfg = resolveConfig({ datasetName: "testds", recallMode: "pinned" });

    it("defaults to auto for the default dataset and to pinned for a non-default one; unknown values count as unset", () => {
      expect(resolveConfig({}).recallMode).toBe("auto");
      expect(resolveConfig({ datasetName: "agent_sessions" }).recallMode).toBe("auto");
      expect(resolveConfig({ datasetName: "testds" }).recallMode).toBe("pinned");
      expect(configPinsRecall(resolveConfig({ datasetName: "testds" }))).toBe(true);
      expect(datasetPinsRecall({ datasetName: "testds" }, resolveConfig({ datasetName: "testds" }))).toBe(true);
      expect(datasetPinsRecall({}, resolveConfig({}))).toBe(false);
      expect(datasetPinsRecall({ datasetName: "testds", recallMode: "pinned" }, pinnedCfg)).toBe(false);
      expect(resolveConfig({ datasetName: "testds", recallMode: "auto" }).recallMode).toBe("auto");
      expect(resolveConfig({ datasetName: "testds", recallMode: "bogus" } as never).recallMode).toBe("pinned");
      expect(resolveConfig({ recallMode: "bogus" } as never).recallMode).toBe("auto");
      expect(resolveConfig({ recallMode: " Pinned " } as never).recallMode).toBe("pinned");
      expect(configPinsRecall(pinnedCfg)).toBe(true);
      for (const name of ["auto", "AUTO", " Auto "]) {
        const auto = resolveConfig({ datasetName: name });
        expect(auto.datasetName).toBe("agent_sessions");
        expect(auto.recallMode).toBe("auto");
      }
    });

    it("COGNEE_PLUGIN_RECALL_MODE applies when the config sets none", () => {
      const prev = process.env.COGNEE_PLUGIN_RECALL_MODE;
      const prevDataset = process.env.COGNEE_PLUGIN_DATASET;
      process.env.COGNEE_PLUGIN_RECALL_MODE = "pinned";
      try {
        expect(resolveConfig({}).recallMode).toBe("pinned");
        expect(resolveConfig({ recallMode: "auto" }).recallMode).toBe("auto");
        process.env.COGNEE_PLUGIN_RECALL_MODE = "auto";
        expect(resolveConfig({ datasetName: "testds" }).recallMode).toBe("auto");
        expect(datasetPinsRecall({ datasetName: "testds" }, resolveConfig({ datasetName: "testds" }))).toBe(false);
        delete process.env.COGNEE_PLUGIN_RECALL_MODE;
        process.env.COGNEE_PLUGIN_DATASET = "proj_x";
        expect(resolveConfig({}).recallMode).toBe("pinned");
        expect(datasetPinsRecall({}, resolveConfig({}))).toBe(true);
      } finally {
        if (prev === undefined) delete process.env.COGNEE_PLUGIN_RECALL_MODE;
        else process.env.COGNEE_PLUGIN_RECALL_MODE = prev;
        if (prevDataset === undefined) delete process.env.COGNEE_PLUGIN_DATASET;
        else process.env.COGNEE_PLUGIN_DATASET = prevDataset;
      }
    });

    it("a conversation's auto lifts the config pin for that conversation only", async () => {
      await run(deps({ cfg: pinnedCfg }), { action: "auto" });
      expect(store.recallPinned(ctx, true)).toBe(false);
      expect(store.recallPinned({ sessionKey: "agent:will:other" }, true)).toBe(true);
    });

    it("pin over the config pin is the conversation's own choice, and reset returns to the config", async () => {
      const d = deps({ cfg: pinnedCfg });
      expect(await run(d, { action: "pin" })).toMatchObject({ changed: false, recall: "pinned", reason: "dataset_picked" });
      expect(store.recallMode(ctx)).toBe("pinned");
      await run(d, { action: "auto" });
      const out = await run(d, { action: "reset" });
      expect(out).toMatchObject({ action: "reset", reset: true, dataset: "testds", recall: "pinned", reason: "configured_pin" });
      expect(store.has(ctx)).toBe(false);
      expect(await run(d, { action: "reset" })).toMatchObject({ reset: false, recall: "pinned", reason: "configured_pin" });
    });

    it("a legacy override (no recall field) stays pinned whatever the config", async () => {
      const { writeFile } = await import("node:fs/promises");
      const legacyPath = join(dir, "legacy.json");
      await writeFile(legacyPath, JSON.stringify({ "key:a": { dataset: "proj-a", sessionSuffix: 2, switchedAt: "", previous: ["agent_sessions"], retired: [] } }));
      const legacy = new DatasetSwitchStore({ path: legacyPath });
      await legacy.ready();
      expect((await loadDatasetOverrides(legacyPath))["key:a"]).not.toHaveProperty("recall");
      expect(legacy.recallPinned({ sessionKey: "a" }, false)).toBe(true);
      expect(legacy.get({ sessionKey: "a" })?.dataset).toBe("proj-a");
    });
  });

  it("reset returns to the configured dataset", async () => {
    const d = deps();
    await run(d, { action: "switch", dataset: "proj-a" });
    expect(await run(d, { action: "reset" })).toMatchObject({ action: "reset", reset: true, dataset: "agent_sessions", recall: "auto" });
    expect(await run(d, { action: "reset" })).toMatchObject({ reset: false, dataset: "agent_sessions" });
  });
});
