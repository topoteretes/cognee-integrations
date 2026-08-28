/**
 * Harness-noise filtering end-to-end: heartbeat/cron/system template prompts
 * must trigger neither auto-recall (LLM-backed search per scope) nor QA
 * capture (templates would be bridged into the permanent graph by improve),
 * while real user prompts keep both. Session registration is lifecycle
 * bookkeeping and must keep running even on harness turns.
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

// Never touch the real shared ~/.cognee-plugin/recall-breaker.json in tests.
const mockBreaker = {
  openForSeconds: jest.fn(async () => 0),
  recordFailure: jest.fn(async (_msg: string) => {}),
  recordSuccess: jest.fn(async () => {}),
};
jest.mock("../../src/breaker", () => ({
  RecallBreaker: jest.fn(() => mockBreaker),
  isBreakerError: () => false,
}));

// In-memory dataset state so these tests never touch ~/.openclaw on disk.
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
      captureSession: true,
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

  const emit = async (name: string, event: unknown, ctx: unknown) => {
    for (const fn of handlers.get(name) ?? []) {
      await fn(event, ctx);
    }
  };
  return { api, emit };
}

/** Let fire-and-forget promise chains settle. */
async function flush(rounds = 10): Promise<void> {
  for (let i = 0; i < rounds; i++) {
    await new Promise((r) => setTimeout(r, 0));
  }
}

const HEARTBEAT_PROMPT =
  "Read HEARTBEAT.md if it exists (workspace context). Follow it strictly. " +
  "Do not infer or repeat old tasks from prior chats. If nothing needs attention, reply HEARTBEAT_OK.";

function qaCalls(): unknown[] {
  return mockRememberEntry.mock.calls
    .map((c) => c[0] as { entry?: { type?: string } })
    .filter((p) => p.entry?.type === "qa");
}

beforeEach(() => {
  jest.clearAllMocks();
  datasetState = { testds: "ds-1" };
  mockBreaker.openForSeconds.mockImplementation(async () => 0);
  mockRecall.mockImplementation(async () => []);
  mockRememberEntry.mockImplementation(async () => ({ entryId: "e1" }));
  mockRegisterAgent.mockImplementation(async () => ({ ok: true, connectionId: "c1" }));
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

describe("harness-noise filtering", () => {
  it("a heartbeat-triggered turn recalls nothing and captures no QA, but still registers the session", async () => {
    const { emit } = createApi();
    const ctx = { agentId: "will", sessionId: "s1", trigger: "heartbeat" };

    await emit("before_prompt_build", { prompt: "check on the deploy status please" }, ctx);
    await emit("llm_output", { assistantTexts: ["HEARTBEAT_OK"] }, ctx);
    await flush();

    expect(mockRecall).not.toHaveBeenCalled();
    expect(qaCalls()).toHaveLength(0);
    expect(mockRegisterAgent).toHaveBeenCalledTimes(1);
  });

  it("the default heartbeat template is filtered by shape when ctx.trigger is absent", async () => {
    const { emit } = createApi();
    const ctx = { agentId: "will", sessionId: "s1" };

    await emit("before_prompt_build", { prompt: HEARTBEAT_PROMPT }, ctx);
    await emit("llm_output", { assistantTexts: ["HEARTBEAT_OK"] }, ctx);
    await flush();

    expect(mockRecall).not.toHaveBeenCalled();
    expect(qaCalls()).toHaveLength(0);
  });

  it("a normal user prompt still recalls and captures its QA", async () => {
    const { emit } = createApi();
    const ctx = { agentId: "will", sessionId: "s1", trigger: "user" };

    await emit("before_prompt_build", { prompt: "what did we decide about the rollout?" }, ctx);
    await emit("llm_output", { assistantTexts: ["we ship on Friday"] }, ctx);
    await flush();

    // Graph lane + the explicit session-layers lane (sessions are on here).
    expect(mockRecall).toHaveBeenCalledTimes(2);
    const qas = qaCalls();
    expect(qas).toHaveLength(1);
    expect(qas[0]).toMatchObject({
      entry: { type: "qa", question: "what did we decide about the rollout?", answer: "we ship on Friday" },
    });
  });

  it("a heartbeat turn clears an unanswered earlier prompt instead of pairing it with the heartbeat's answer", async () => {
    const { emit } = createApi();
    const ctx = { agentId: "will", sessionId: "s1" };

    // User prompt whose run died before any llm_output…
    await emit("before_prompt_build", { prompt: "summarize the incident timeline" }, ctx);
    // …then a heartbeat turn answers. Its output must not become the "answer".
    await emit("before_prompt_build", { prompt: HEARTBEAT_PROMPT }, ctx);
    await emit("llm_output", { assistantTexts: ["HEARTBEAT_OK"] }, ctx);
    await flush();

    expect(qaCalls()).toHaveLength(0);
  });

  it("noiseTriggers/noisePatterns set to [] disable filtering entirely", async () => {
    const { emit } = createApi({ noiseTriggers: [], noisePatterns: [] });
    const ctx = { agentId: "will", sessionId: "s1", trigger: "heartbeat" };

    await emit("before_prompt_build", { prompt: HEARTBEAT_PROMPT }, ctx);
    await emit("llm_output", { assistantTexts: ["HEARTBEAT_OK"] }, ctx);
    await flush();

    expect(mockRecall).toHaveBeenCalledTimes(2); // graph lane + session-layers lane
    expect(qaCalls()).toHaveLength(1);
  });

  it("custom noisePatterns extend filtering to host-specific templates", async () => {
    const { emit } = createApi({ noisePatterns: [String.raw`^\[digest\]`] });
    const ctx = { agentId: "will", sessionId: "s1" };

    await emit("before_prompt_build", { prompt: "[digest] compile the morning digest" }, ctx);
    await flush();
    expect(mockRecall).not.toHaveBeenCalled();

    // Custom patterns REPLACE the defaults, so the stock heartbeat template
    // now goes through — the trigger layer is what still catches real
    // heartbeat runs in this configuration.
    await emit("before_prompt_build", { prompt: HEARTBEAT_PROMPT }, { ...ctx, sessionId: "s2" });
    await flush();
    expect(mockRecall).toHaveBeenCalledTimes(2); // graph lane + session-layers lane
  });
});
