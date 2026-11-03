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
  MAX_SYNCED_RETIRED,
  configPinsRecall,
  conversationKeys,
  createDatasetSwitchTool,
  isValidDatasetName,
  loadDatasetOverrides,
  pruneRetired,
  withSessionSuffix,
  type DatasetSwitchDeps,
  type MemorySwitchDatasetResult,
} from "../../src/dataset-switch";

// The default dataset: recall is auto unless a conversation pins it (a
// non-default configured dataset pins it — see "config datasetName pins recall").
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
  });

  it("treats a legacy override with no recall field as pinned", async () => {
    const { writeFile } = await import("node:fs/promises");
    await writeFile(path, JSON.stringify({ "key:a": { dataset: "proj-a", sessionSuffix: 2, switchedAt: "", previous: ["testds"], retired: [] } }));
    const loaded = await loadDatasetOverrides(path);
    expect(loaded["key:a"]).not.toHaveProperty("recall");
    const store = new DatasetSwitchStore({ path });
    await store.ready();
    expect(store.recallPinned({ sessionKey: "a" })).toBe(true);
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
    const loaded = (await loadDatasetOverrides(path))["key:k"].retired;
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
    expect(ok).toEqual({ action: "reset", reset: true, dataset: "agent_sessions", resynced: ["open_claw_s1"] });
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

  it("rejects bad input, and switching to the dataset in use only restricts recall", async () => {
    const d = deps();
    expect((await run(d, { action: "switch" })) as { error: string }).toMatchObject({ switched: false, error: "dataset is required" });
    expect(((await run(d, { action: "switch", dataset: "bad name!" })) as { error: string }).error).toMatch(/invalid dataset name/);
    expect(await run(d, { action: "switch", dataset: "agent_sessions" })).toMatchObject({ switched: false, reason: "recall_restricted", recall: "pinned" });
    expect(store.recallPinned(ctx)).toBe(true);
    // A pin-only override keeps the session id: no suffix.
    expect(withSessionSuffix("open_claw_s1", store.get(ctx))).toBe("open_claw_s1");
    expect(await run(d, { action: "switch", dataset: "agent_sessions" })).toMatchObject({ switched: false, reason: "already_active" });
    expect(((await run(d, { action: "switch", dataset: "proj-a" }, { agentId: "will" })) as { error: string }).error).toMatch(/no conversation\/session/);
    expect(d.synced).toEqual([]);
    expect(d.ensured).toEqual([]);
  });

  it("reserves \"auto\": a switch to it is an error and changes nothing", async () => {
    const d = deps();
    for (const name of ["auto", " Auto ", "AUTO"]) {
      const out = await run(d, { action: "switch", dataset: name });
      expect(out).toMatchObject({ action: "switch", switched: false, dataset: "agent_sessions" });
      expect((out as { error: string }).error).toMatch(/"auto" is reserved.*action="auto"/);
    }
    expect(store.get(ctx)).toBeUndefined();
    expect(d.synced).toEqual([]);
    expect(d.ensured).toEqual([]);
  });

  it("reports recall as pinned when unpinned recall does not route, and action=auto still saves the choice", async () => {
    const d = deps({ autoRecall: async () => false });
    const cur = await run(d, { action: "current" });
    expect(cur).toMatchObject({ dataset: "agent_sessions", recall: "pinned", switched: false });
    expect((cur as { note: string }).note).toBe(`This server can't route yet, so recall stays on "agent_sessions".`);

    await run(d, { action: "switch", dataset: "proj-a" });
    expect(await run(d, { action: "auto" })).toEqual({
      action: "auto",
      dataset: "proj-a",
      changed: true,
      recall: "pinned",
      note: `Saved. This server can't route yet, so recall stays on "proj-a".`,
    });
    // The choice is stored: recall routes once the server can.
    expect(store.recallPinned(ctx)).toBe(false);
    expect(await run(deps({ autoRecall: async () => true }), { action: "current" })).toMatchObject({ recall: "auto" });
    // A failed probe counts as not routing.
    expect(await run(deps({ autoRecall: async () => { throw new Error("down"); } }), { action: "current" })).toMatchObject({ recall: "pinned" });
  });

  it("in multi-scope mode reports recall as restricted to the configured scopes", async () => {
    const multi = resolveConfig({ datasetName: "agent_sessions", companyDataset: "acme", recallScopes: ["company", "agent"] });
    const d = deps({ cfg: multi, autoRecall: async () => true });
    const cur = await run(d, { action: "current" });
    expect(cur).toMatchObject({ recall: "pinned", scopes: ["company", "agent"] });
    expect((cur as { note: string }).note).toMatch(/configured scopes \(company, agent\)/);
    expect(await run(d, { action: "auto" })).toMatchObject({ changed: false, recall: "pinned" });
  });

  it("recall is auto until a switch, and action=auto lifts the restriction without moving saves (SDK-1393)", async () => {
    const d = deps();
    expect(await run(d, { action: "current" })).toMatchObject({ recall: "auto", switched: false });
    expect(await run(d, { action: "auto" })).toMatchObject({ action: "auto", changed: false, recall: "auto" });

    await run(d, { action: "switch", dataset: "proj-a" });
    expect(store.recallPinned(ctx)).toBe(true);
    expect(await run(d, { action: "current" })).toMatchObject({ dataset: "proj-a", recall: "pinned", switched: true });

    expect(await run(d, { action: "auto" })).toMatchObject({ action: "auto", dataset: "proj-a", changed: true, recall: "auto" });
    expect(store.recallPinned(ctx)).toBe(false);
    // Saves still go to the switched dataset.
    expect(store.get(ctx)?.dataset).toBe("proj-a");
  });

  it("action=auto drops a pin-only override, and the recall mode survives a reload", async () => {
    const d = deps();
    await run(d, { action: "switch", dataset: "agent_sessions" });
    await store.flush();
    const reloaded = new DatasetSwitchStore({ path: join(dir, "o.json") });
    await reloaded.ready();
    expect(reloaded.recallPinned(ctx)).toBe(true);
    expect(reloaded.get(ctx)?.sessionSuffix).toBe(1);

    expect(await run(d, { action: "auto" })).toMatchObject({ changed: true, recall: "auto" });
    expect(store.get(ctx)).toBeUndefined();
  });

  it("surfaces a failed ensureDataset without recording the override", async () => {
    const d = deps({ ensureDataset: async () => { throw new Error("403"); } });
    const out = await run(d, { action: "switch", dataset: "proj-a" });
    expect((out as { error: string }).error).toMatch(/could not create\/resolve dataset "proj-a"/);
    expect(store.get(ctx)).toBeUndefined();
  });

  describe("config datasetName pins recall", () => {
    // An explicitly configured, non-default dataset scopes the agent's memory:
    // recall stays on it unless the conversation picks auto (hook-family rule).
    const pinnedCfg = resolveConfig({ datasetName: "testds" });

    it("only a non-default dataset pins; the default and \"auto\" leave recall on auto", () => {
      expect(configPinsRecall(pinnedCfg)).toBe(true);
      expect(configPinsRecall(resolveConfig({}))).toBe(false);
      expect(configPinsRecall(resolveConfig({ datasetName: "agent_sessions" }))).toBe(false);
      for (const name of ["auto", "AUTO", " Auto "]) {
        const auto = resolveConfig({ datasetName: name });
        expect(auto.datasetName).toBe("agent_sessions");
        expect(configPinsRecall(auto)).toBe(false);
      }
    });

    it("COGNEE_PLUGIN_DATASET=auto means the default dataset with auto recall", () => {
      const prev = process.env.COGNEE_PLUGIN_DATASET;
      process.env.COGNEE_PLUGIN_DATASET = "auto";
      try {
        const resolved = resolveConfig({ datasetName: "testds" });
        expect(resolved.datasetName).toBe("agent_sessions");
        expect(configPinsRecall(resolved)).toBe(false);
      } finally {
        if (prev === undefined) delete process.env.COGNEE_PLUGIN_DATASET;
        else process.env.COGNEE_PLUGIN_DATASET = prev;
      }
    });

    it("current reports the config pin and says why", async () => {
      const cur = await run(deps({ cfg: pinnedCfg, autoRecall: async () => true }), { action: "current" });
      expect(cur).toMatchObject({ dataset: "testds", recall: "pinned", switched: false });
      expect((cur as { note: string }).note).toMatch(/pinned by config datasetName "testds".*action=auto unpins it for this conversation/);
      expect(store.recallPinned(ctx, true)).toBe(true);
      expect(store.recallPinned(ctx)).toBe(false);
    });

    it("action=auto lifts the config pin for this conversation only, and the choice survives a reload", async () => {
      const d = deps({ cfg: pinnedCfg, autoRecall: async () => true });
      const out = await run(d, { action: "auto" });
      expect(out).toMatchObject({ action: "auto", dataset: "testds", changed: true, recall: "auto" });
      expect((out as { note: string }).note).toMatch(/lifts the config datasetName "testds" pin for this conversation only/);
      // A pin-only-shaped override records the choice: same session, same dataset.
      expect(store.get(ctx)).toMatchObject({ dataset: "testds", sessionSuffix: 1, recall: "auto" });
      expect(withSessionSuffix("open_claw_s1", store.get(ctx))).toBe("open_claw_s1");
      expect(store.recallPinned(ctx, true)).toBe(false);
      // Another conversation stays pinned by the config.
      expect(store.recallPinned({ sessionKey: "agent:will:other" }, true)).toBe(true);
      expect(await run(d, { action: "auto" })).toMatchObject({ changed: false, recall: "auto", note: "Recall was already auto." });
      expect(await run(d, { action: "current" })).toMatchObject({ recall: "auto" });

      await store.flush();
      const reloaded = new DatasetSwitchStore({ path: join(dir, "o.json") });
      await reloaded.ready();
      expect(reloaded.recallMode(ctx)).toBe("auto");
      expect(reloaded.recallPinned(ctx, true)).toBe(false);
    });

    it("picking the dataset in use pins again, and reset returns to the config pin", async () => {
      const d = deps({ cfg: pinnedCfg, autoRecall: async () => true });
      await run(d, { action: "auto" });
      expect(await run(d, { action: "switch", dataset: "testds" })).toMatchObject({ switched: false, reason: "recall_restricted", recall: "pinned" });
      expect(store.recallMode(ctx)).toBe("pinned");
      // Unpinning over a config pin keeps an "auto" override instead of dropping it.
      await run(d, { action: "auto" });
      expect(store.recallMode(ctx)).toBe("auto");

      const out = await run(d, { action: "reset" });
      expect(out).toMatchObject({ action: "reset", reset: true, dataset: "testds" });
      expect((out as { note: string }).note).toMatch(/pinned by config datasetName "testds"/);
      expect(store.get(ctx)).toBeUndefined();
      expect(await run(d, { action: "current" })).toMatchObject({ recall: "pinned" });
    });

    it("an explicit pick over the config pin is recorded, and a legacy override stays pinned", async () => {
      const d = deps({ cfg: pinnedCfg, autoRecall: async () => true });
      expect(await run(d, { action: "switch", dataset: "testds" })).toMatchObject({ reason: "recall_restricted", recall: "pinned" });
      expect(store.get(ctx)).toMatchObject({ sessionSuffix: 1, recall: "pinned" });
      expect(await run(d, { action: "switch", dataset: "testds" })).toMatchObject({ reason: "already_active" });

      const { writeFile } = await import("node:fs/promises");
      const legacyPath = join(dir, "legacy.json");
      await writeFile(legacyPath, JSON.stringify({ "key:a": { dataset: "proj-a", sessionSuffix: 2, switchedAt: "", previous: ["agent_sessions"], retired: [] } }));
      const legacy = new DatasetSwitchStore({ path: legacyPath });
      await legacy.ready();
      expect(legacy.recallPinned({ sessionKey: "a" }, false)).toBe(true);
    });

    it("multi-scope mode ignores the config rule", async () => {
      const multi = resolveConfig({ datasetName: "testds", companyDataset: "acme", recallScopes: ["company", "agent"] });
      const d = deps({ cfg: multi, autoRecall: async () => true });
      const cur = await run(d, { action: "current" });
      expect((cur as { note: string }).note).not.toMatch(/config datasetName/);
      expect(await run(d, { action: "auto" })).toMatchObject({ changed: false, recall: "pinned" });
      expect(store.get(ctx)).toBeUndefined();
    });
  });

  it("reset returns to the configured dataset", async () => {
    const d = deps();
    await run(d, { action: "switch", dataset: "proj-a" });
    expect(await run(d, { action: "reset" })).toEqual({ action: "reset", reset: true, dataset: "agent_sessions" });
    expect(await run(d, { action: "reset" })).toMatchObject({ reset: false, dataset: "agent_sessions" });
  });
});
