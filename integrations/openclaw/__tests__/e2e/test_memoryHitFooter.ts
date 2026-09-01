/**
 * Memory-hit footer + weekly digest end-to-end through the plugin hooks:
 * a turn whose recall injected memories gets a footer on its FINAL reply
 * payload, a turn with no hits gets nothing, chunked/tool payloads pass
 * through, the footer is formatted per config, and each feature has an off
 * switch. The digest rides the same hook once the week closes.
 */

import plugin from "../../src/plugin";
import { CogneeHttpClient } from "../../src/client";

jest.mock("../../src/client");
jest.mock("../../src/server", () => ({
  bootServerIfNeeded: jest.fn(async () => {}),
  waitForServerHealth: jest.fn(async () => {}),
  isLocalUrl: jest.fn(() => true),
  resolveOrMintApiKey: jest.fn(async () => "test-api-key"),
  spawnExitWatcher: jest.fn(async () => {}),
  exitWatcherPidfilePath: jest.fn((name: string) => `/tmp/exit-watchers/${name}.pid`),
}));

const mockBreaker = {
  openForSeconds: jest.fn(async () => 0),
  recordFailure: jest.fn(async (_msg: string) => {}),
  recordSuccess: jest.fn(async () => {}),
};
jest.mock("../../src/breaker", () => ({
  RecallBreaker: jest.fn(() => mockBreaker),
  isBreakerError: () => false,
}));

let datasetState: Record<string, string> = {};
jest.mock("../../src/persistence", () => ({
  loadDatasetState: jest.fn(async () => ({ ...datasetState })),
  saveDatasetState: jest.fn(async (s: Record<string, string>) => { datasetState = { ...s }; }),
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

// The digest tracker's clock, advanced by tests to close a week. Only `now`
// is stubbed; counters and the (sandboxed-home) stats file stay real.
let nowMs = 1_700_000_000_000;
jest.mock("../../src/digest", () => {
  const actual = jest.requireActual<typeof import("../../src/digest")>("../../src/digest");
  class ClockedTracker extends actual.DigestTracker {
    constructor(opts: ConstructorParameters<typeof actual.DigestTracker>[0] = {}) {
      super({ ...opts, now: () => nowMs });
    }
  }
  return { ...actual, DigestTracker: ClockedTracker };
});

const mockRecall = jest.fn(async (_params: unknown): Promise<unknown[]> => []);
const mockRememberEntry = jest.fn(async (_params: unknown): Promise<{ entryId?: string }> => ({ entryId: "e1" }));
const mockRegisterAgent = jest.fn(async (_params: unknown) => ({ ok: true, connectionId: "c1" }));

type HookHandler = (event: unknown, ctx: unknown) => Promise<unknown> | unknown;

function createApi(extraConfig: Record<string, unknown> = {}) {
  const handlers = new Map<string, HookHandler[]>();
  const api = {
    id: "cognee-openclaw",
    name: "Memory (Cognee)",
    source: "test",
    config: {},
    pluginConfig: {
      autoIndex: false,
      autoRecall: true,
      enableSessions: true,
      captureSession: false,
      datasetName: "testds",
      minScore: 0,
      ...extraConfig,
    },
    runtime: {},
    logger: { info: jest.fn(), warn: jest.fn(), debug: jest.fn() },
    registerMemoryFlushPlan: jest.fn(),
    registerCli: jest.fn(),
    registerService: jest.fn(),
    on: jest.fn((name: string, fn: HookHandler) => {
      const list = handlers.get(name) ?? [];
      list.push(fn);
      handlers.set(name, list);
    }),
  };
  plugin.register(api as never);

  /** Emit and return the LAST handler's result (the plugin registers one per hook). */
  const emit = async (name: string, event: unknown, ctx: unknown): Promise<unknown> => {
    let result: unknown;
    for (const fn of handlers.get(name) ?? []) result = await fn(event, ctx);
    return result;
  };
  return { api, emit, subscribed: () => [...handlers.keys()] };
}

const MEMORIES = [
  { id: "m1", text: "User prefers dark mode", score: 0.9, metadata: { source: "MEMORY.md" } },
  { id: "m2", text: "Deploy is on Fridays", score: 0.8, metadata: { name: "memory/ops.md.txt" } },
  { id: "m3", text: "Q3 goal is retention", score: 0.7 },
];

function finalPayload(text: string, extra: Record<string, unknown> = {}) {
  return { payload: { text }, kind: "final", ...extra };
}

beforeEach(() => {
  jest.clearAllMocks();
  datasetState = { testds: "ds-1" };
  nowMs = 1_700_000_000_000;
  mockBreaker.openForSeconds.mockImplementation(async () => 0);
  mockRecall.mockImplementation(async () => []);
  (CogneeHttpClient as unknown as jest.Mock).mockImplementation(() => ({
    recall: mockRecall,
    rememberEntry: mockRememberEntry,
    registerAgent: mockRegisterAgent,
    unregisterAgent: jest.fn(async () => ({ ok: true, activeAgents: 0 })),
    improve: jest.fn(async () => ({ status: "ok" })),
    health: jest.fn(async () => ({ status: "ok" })),
    listDatasets: jest.fn(async () => []),
    setApiKey: jest.fn(),
  }));
});

describe("per-turn memory-hit footer", () => {
  it("appends the footer to the final reply of a turn that injected memories", async () => {
    mockRecall.mockImplementation(async () => MEMORIES);
    const { emit } = createApi();
    const ctx = { agentId: "will", sessionId: "s1", sessionKey: "agent:will:main", runId: "run-1" };

    const injection = await emit("before_prompt_build", { prompt: "what theme does the user like?" }, ctx);
    expect(injection).toBeDefined();

    const result = await emit("reply_payload_sending", finalPayload("Dark mode, always.", { runId: "run-1", sessionKey: "agent:will:main" }), ctx);
    expect(result).toEqual({ payload: { text: "Dark mode, always.\n\n[cognee: 3 memories]" } });
  });

  it("adds nothing when recall found no memories", async () => {
    const { emit } = createApi();
    const ctx = { agentId: "will", sessionId: "s1", sessionKey: "agent:will:main", runId: "run-1" };

    await emit("before_prompt_build", { prompt: "what theme does the user like?" }, ctx);
    const result = await emit("reply_payload_sending", finalPayload("No idea.", { runId: "run-1" }), ctx);
    expect(result).toBeUndefined();
  });

  it("adds nothing when the turn never ran recall (harness noise)", async () => {
    mockRecall.mockImplementation(async () => MEMORIES);
    const { emit } = createApi();
    const ctx = { agentId: "will", sessionId: "s1", runId: "run-hb", trigger: "heartbeat" };

    await emit("before_prompt_build", { prompt: "check on the deploy" }, ctx);
    expect(mockRecall).not.toHaveBeenCalled();
    expect(await emit("reply_payload_sending", finalPayload("HEARTBEAT_OK", { runId: "run-hb" }), ctx)).toBeUndefined();
  });

  it("skips tool/block chunks and footers only the final payload, exactly once", async () => {
    mockRecall.mockImplementation(async () => MEMORIES);
    const { emit } = createApi();
    const ctx = { agentId: "will", sessionId: "s1", runId: "run-1" };

    await emit("before_prompt_build", { prompt: "what theme does the user like?" }, ctx);
    expect(await emit("reply_payload_sending", { payload: { text: "thinking…" }, kind: "block", runId: "run-1" }, ctx)).toBeUndefined();
    expect(await emit("reply_payload_sending", { payload: { text: "ran grep" }, kind: "tool", runId: "run-1" }, ctx)).toBeUndefined();

    const final = await emit("reply_payload_sending", finalPayload("Dark mode.", { runId: "run-1" }), ctx);
    expect((final as { payload: { text: string } }).payload.text).toMatch(/\[cognee: 3 memories\]$/);

    // A second final for the same run (re-delivery) must not footer again.
    expect(await emit("reply_payload_sending", finalPayload("Dark mode.", { runId: "run-1" }), ctx)).toBeUndefined();
  });

  it("correlates on sessionKey when the reply hook carries no runId", async () => {
    mockRecall.mockImplementation(async () => MEMORIES.slice(0, 1));
    const { emit } = createApi();

    await emit("before_prompt_build", { prompt: "what theme does the user like?" }, { agentId: "will", sessionId: "s1", sessionKey: "agent:will:main" });
    const result = await emit("reply_payload_sending", finalPayload("Dark.", { sessionKey: "agent:will:main" }), { channelId: "telegram", sessionKey: "agent:will:main" });
    expect(result).toEqual({ payload: { text: "Dark.\n\n[cognee: 1 memory]" } });
  });

  it("leaves media-only payloads alone and keeps the hit for the next text payload", async () => {
    mockRecall.mockImplementation(async () => MEMORIES);
    const { emit } = createApi();
    const ctx = { agentId: "will", sessionId: "s1", runId: "run-1" };

    await emit("before_prompt_build", { prompt: "show me the chart" }, ctx);
    expect(await emit("reply_payload_sending", { payload: { mediaUrl: "http://x/chart.png" }, kind: "final", runId: "run-1" }, ctx)).toBeUndefined();
    const result = await emit("reply_payload_sending", finalPayload("Here it is.", { runId: "run-1" }), ctx);
    expect((result as { payload: { text: string } }).payload.text).toMatch(/\[cognee: 3 memories\]$/);
  });

  it("honours a custom format with {sources}", async () => {
    mockRecall.mockImplementation(async () => MEMORIES);
    const { emit } = createApi({ memoryHitFooterFormat: "— {count} {memories} via {sources}" });
    const ctx = { agentId: "will", sessionId: "s1", runId: "run-1" };

    await emit("before_prompt_build", { prompt: "what theme does the user like?" }, ctx);
    const result = await emit("reply_payload_sending", finalPayload("Dark.", { runId: "run-1" }), ctx);
    expect(result).toEqual({ payload: { text: "Dark.\n\n— 3 memories via MEMORY.md, ops.md, memory" } });
  });

  it("is disabled by memoryHitFooter: false", async () => {
    mockRecall.mockImplementation(async () => MEMORIES);
    const { emit } = createApi({ memoryHitFooter: false, weeklyDigest: true });
    const ctx = { agentId: "will", sessionId: "s1", runId: "run-1" };

    await emit("before_prompt_build", { prompt: "what theme does the user like?" }, ctx);
    expect(await emit("reply_payload_sending", finalPayload("Dark.", { runId: "run-1" }), ctx)).toBeUndefined();
  });

  it("registers no reply hook at all when both features are off", () => {
    const { subscribed } = createApi({ memoryHitFooter: false, weeklyDigest: false });
    expect(subscribed()).not.toContain("reply_payload_sending");
  });

  it("registers no reply hook when autoRecall is off (nothing to report)", () => {
    const { subscribed } = createApi({ autoRecall: false });
    expect(subscribed()).not.toContain("reply_payload_sending");
  });
});

describe("weekly digest", () => {
  const WEEK = 7 * 24 * 60 * 60 * 1000;

  async function runTurn(emit: ReturnType<typeof createApi>["emit"], runId: string, hits: unknown[]) {
    mockRecall.mockImplementation(async () => hits);
    const ctx = { agentId: "will", sessionId: "s1", runId };
    await emit("before_prompt_build", { prompt: `question ${runId}` }, ctx);
    return emit("reply_payload_sending", finalPayload("answer", { runId }), ctx);
  }

  it("appends the digest once the week closes, then not again until the next week", async () => {
    const { emit } = createApi({ memoryHitFooter: false });

    await runTurn(emit, "r1", MEMORIES);
    await runTurn(emit, "r2", []);
    await runTurn(emit, "r3", MEMORIES.slice(0, 1));
    expect(await runTurn(emit, "r4", [])).toBeUndefined(); // window still open

    // The delivering turn recalls before it replies, so it counts too (5 turns).
    nowMs += WEEK;
    const result = await runTurn(emit, "r5", []);
    expect(result).toEqual({
      payload: {
        text: "answer\n\n[cognee weekly digest] This week cognee found relevant memories on 2 of your agent's 5 turns (top sources: MEMORY.md, memory, ops.md).",
      },
    });

    // Fresh window: no digest on the very next reply.
    expect(await runTurn(emit, "r6", [])).toBeUndefined();
  });

  it("is suppressed when the week had zero hits", async () => {
    const { emit } = createApi({ memoryHitFooter: false });
    await runTurn(emit, "r1", []);
    await runTurn(emit, "r2", []);
    nowMs += WEEK;
    expect(await runTurn(emit, "r3", [])).toBeUndefined();
  });

  it("stacks under the footer when both fire on the same reply", async () => {
    const { emit } = createApi();
    await runTurn(emit, "r1", MEMORIES);
    nowMs += WEEK;
    const result = await runTurn(emit, "r2", MEMORIES.slice(0, 1));
    const text = (result as { payload: { text: string } }).payload.text;
    expect(text.split("\n")).toEqual([
      "answer",
      "",
      "[cognee: 1 memory]",
      expect.stringMatching(/^\[cognee weekly digest\] .*2 of your agent's 2 turns/),
    ]);
  });

  it("is disabled by weeklyDigest: false", async () => {
    const { emit } = createApi({ memoryHitFooter: false, weeklyDigest: false });
    await runTurn(emit, "r1", MEMORIES);
    nowMs += WEEK;
    expect(await runTurn(emit, "r2", MEMORIES)).toBeUndefined();
  });
});
