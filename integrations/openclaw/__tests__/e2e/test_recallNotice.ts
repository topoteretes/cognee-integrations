/**
 * One-time recall notice (SDK-1393): a configured non-default dataset with no
 * recall mode set keeps recall pinned, and the plugin says once per dataset —
 * a trailer on the first final reply plus one log line — that auto recall
 * exists. The marker lives in the plugin's state dir (the sandboxed home), so
 * the notice stays shown across a gateway restart. Default-dataset users and
 * users who set a recall mode never see it.
 */

import { rm } from "node:fs/promises";
import plugin from "../../src/plugin";
import { CogneeHttpClient } from "../../src/client";
import { DatasetSwitchStore } from "../../src/dataset-switch";
import { loadRecallNotices, recallNoticesPath, recallPinNotice } from "../../src/recall-notice";
import { createPluginApi } from "../../test-utils/fakeApi";

jest.mock("../../src/client");
jest.mock("../../src/server", () => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const { serverMock: mk } = require("../../test-utils/fakeApi");
  return mk();
});

jest.mock("../../src/breaker", () => ({
  RecallBreaker: jest.fn(() => ({
    openForSeconds: jest.fn(async () => 0),
    recordFailure: jest.fn(async () => {}),
    recordSuccess: jest.fn(async () => {}),
  })),
  isBreakerError: () => false,
}));

let savedOverrides: Record<string, unknown> = {};
jest.mock("../../src/persistence", () => ({
  loadDatasetState: jest.fn(async () => ({ agent_sessions: "ds-1", testds: "ds-2", other: "ds-3" })),
  saveDatasetState: jest.fn(async () => {}),
  DATASET_OVERRIDES_PATH: "/tmp/dataset-overrides.json",
  loadDatasetOverrides: jest.fn(async () => JSON.parse(JSON.stringify(savedOverrides))),
  saveDatasetOverrides: jest.fn(async (o: Record<string, unknown>) => { savedOverrides = JSON.parse(JSON.stringify(o)); }),
  loadSyncIndex: jest.fn(async () => ({ entries: {} })),
  saveSyncIndex: jest.fn(async () => {}),
  loadScopedSyncIndexes: jest.fn(async () => ({})),
  saveScopedSyncIndexes: jest.fn(async () => {}),
  loadAgentSyncIndexes: jest.fn(async () => ({})),
  saveAgentSyncIndexes: jest.fn(async () => {}),
  migrateLegacyIndex: jest.fn(async () => null),
  migrateAgentScopeToPerAgent: jest.fn(async () => null),
  SYNC_INDEX_PATH: "/tmp/sync-index.json",
}));

const CONVO = { agentId: "will", sessionId: "s1", sessionKey: "agent:will:tg-1" };
const NOTICE = recallPinNotice("testds");

function harness(extra: Record<string, unknown> = {}) {
  const h = createPluginApi(plugin, { autoRecall: true, enableSessions: false, captureSession: false, memorySteer: false, memoryHitFooter: false, weeklyDigest: false, minScore: 0, ...extra });
  const replyHooks = (h.api.on as jest.Mock).mock.calls.filter((c) => c[0] === "reply_payload_sending").map((c) => c[1]);
  /** Send one final reply through the plugin's reply hook; returns the text the user sees. */
  const reply = async (text = "Here is the answer.", ctx: Record<string, unknown> = CONVO) => {
    let out = text;
    for (const fn of replyHooks) {
      const res = (await fn({ kind: "final", payload: { text: out }, sessionKey: ctx.sessionKey }, ctx)) as { payload?: { text?: string } } | undefined;
      if (res?.payload?.text) out = res.payload.text;
    }
    return out;
  };
  return { ...h, reply };
}

async function run(h: ReturnType<typeof harness>, params: Record<string, unknown>) {
  const tool = h.tools(CONVO).find((t) => t.name === "memory_switch_dataset")!;
  return ((await tool.execute("c", params)) as { details: Record<string, unknown> }).details;
}

/** The marker save is fire-and-forget; wait until it lands. */
async function markerFor(dataset: string): Promise<string | undefined> {
  for (let i = 0; i < 100; i++) {
    const shown = (await loadRecallNotices())[dataset];
    if (shown) return shown;
    await new Promise((r) => setTimeout(r, 5));
  }
  return undefined;
}

const count = (text: string) => text.split(NOTICE).length - 1;

beforeEach(async () => {
  jest.clearAllMocks();
  DatasetSwitchStore.resetShared();
  savedOverrides = {};
  await rm(recallNoticesPath(), { force: true });
  (CogneeHttpClient as unknown as jest.Mock).mockImplementation(() => ({
    recall: jest.fn(async () => []),
    health: jest.fn(async () => ({ status: "ok" })),
    listDatasets: jest.fn(async () => []),
    setApiKey: jest.fn(),
  }));
});

describe("one-time recall notice", () => {
  it("is appended once to the first reply for a non-default dataset with no recall mode, and logged once", async () => {
    const h = harness({ datasetName: "testds" });
    const first = await h.reply();
    expect(first).toBe(`Here is the answer.\n\n${NOTICE}`);
    expect(NOTICE).toBe(
      "Cognee: recall stays on `testds`. Cognee can also pick the best-matching dataset for each prompt from all you can read: " +
      "set `recallMode: \"auto\"` in the plugin config, or ask to set recall to auto for this conversation.",
    );
    expect(await h.reply()).toBe("Here is the answer.");
    expect(await h.reply("Other chat.", { agentId: "will", sessionId: "s2", sessionKey: "agent:will:tg-2" })).toBe("Other chat.");
    expect(h.logger.info.mock.calls.filter((c) => String(c[0]).includes(NOTICE))).toHaveLength(1);
    expect(await markerFor("testds")).toEqual(expect.any(String));
  });

  it("stays shown after a restart (persisted marker), but fires again for another dataset", async () => {
    const h = harness({ datasetName: "testds" });
    expect(count(await h.reply())).toBe(1);
    await markerFor("testds");

    DatasetSwitchStore.resetShared();
    const restarted = harness({ datasetName: "testds" });
    expect(await restarted.reply()).toBe("Here is the answer.");
    expect(restarted.logger.info.mock.calls.some((c) => String(c[0]).includes("recall stays on"))).toBe(false);

    DatasetSwitchStore.resetShared();
    const other = harness({ datasetName: "other" });
    expect(await other.reply()).toBe(`Here is the answer.\n\n${recallPinNotice("other")}`);
    expect(await markerFor("other")).toEqual(expect.any(String));
    expect(await markerFor("testds")).toEqual(expect.any(String));
  });

  it("never shows for the default dataset or when a recall mode is set", async () => {
    for (const extra of [
      { datasetName: "agent_sessions" },
      { datasetName: "testds", recallMode: "auto" },
      { datasetName: "testds", recallMode: "pinned" },
    ]) {
      DatasetSwitchStore.resetShared();
      const h = harness(extra);
      expect(await h.reply()).toBe("Here is the answer.");
      expect(h.logger.info.mock.calls.some((c) => String(c[0]).includes("recall stays on"))).toBe(false);
    }

    const prev = process.env.COGNEE_PLUGIN_RECALL_MODE;
    process.env.COGNEE_PLUGIN_RECALL_MODE = "pinned";
    try {
      DatasetSwitchStore.resetShared();
      expect(await harness({ datasetName: "testds" }).reply()).toBe("Here is the answer.");
    } finally {
      if (prev === undefined) delete process.env.COGNEE_PLUGIN_RECALL_MODE;
      else process.env.COGNEE_PLUGIN_RECALL_MODE = prev;
    }
    expect(await loadRecallNotices()).toEqual({});
  });

  it("never shows in multi-scope mode", async () => {
    const h = harness({ datasetName: "testds", companyDataset: "acme", recallScopes: ["company", "agent"] });
    expect(h.handlerCount("reply_payload_sending")).toBe(0);
  });

  it("waits while the conversation has its own recall choice, then shows once on another conversation", async () => {
    const h = harness({ datasetName: "testds" });
    expect(await run(h, { action: "auto" })).toMatchObject({ recall: "auto" });
    expect(await h.reply()).toBe("Here is the answer.");
    expect(count(await h.reply("Hi.", { agentId: "will", sessionId: "s2", sessionKey: "agent:will:tg-2" }))).toBe(1);
  });
});
