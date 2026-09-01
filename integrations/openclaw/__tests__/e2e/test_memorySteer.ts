/**
 * Memory steer + version surface through the plugin: the steer rides
 * before_agent_start as cached system context on real turns only, honours
 * its off-switch and text override; `cognee version` prints the installed
 * version and an update hint only when the cached npm check is newer.
 */

import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import plugin from "../../src/plugin";
import { CogneeHttpClient } from "../../src/client";
import { DEFAULT_MEMORY_STEER_TEXT } from "../../src/config";
import { createPluginApi } from "../../test-utils/fakeApi";

jest.mock("../../src/client");
jest.mock("../../src/server", () => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const { serverMock: mk } = require("../../test-utils/fakeApi");
  return mk();
});
jest.mock("../../src/breaker", () => ({
  RecallBreaker: jest.fn(() => ({ openForSeconds: async () => 0, recordFailure: async () => {}, recordSuccess: async () => {} })),
  isBreakerError: () => false,
}));

// Point the update-check cache at a per-test file so `version` reads what the
// test wrote, never a developer's real cache.
let updateCachePath = "";
jest.mock("../../src/persistence", () => {
  const actual = jest.requireActual<typeof import("../../src/persistence")>("../../src/persistence");
  return {
    ...actual,
    loadDatasetState: jest.fn(async () => ({ testds: "ds-1" })),
    saveDatasetState: jest.fn(async () => {}),
    loadDatasetOverrides: jest.fn(async () => ({})),
    saveDatasetOverrides: jest.fn(async () => {}),
    get UPDATE_CHECK_PATH() { return updateCachePath; },
  };
});

beforeEach(() => {
  jest.clearAllMocks();
  updateCachePath = join(mkdtempSync(join(tmpdir(), "cognee-steer-")), "update-check.json");
  (CogneeHttpClient as unknown as jest.Mock).mockImplementation(() => ({
    recall: jest.fn(async () => []),
    registerAgent: jest.fn(async () => ({ ok: true, connectionId: "c1" })),
    unregisterAgent: jest.fn(async () => ({ ok: true, activeAgents: 0 })),
    health: jest.fn(async () => ({ status: "ok" })),
    listDatasets: jest.fn(async () => []),
    setApiKey: jest.fn(),
  }));
});

type Handler = (event: unknown, ctx: unknown) => unknown;
function steerHandlers(api: Record<string, unknown>): Handler[] {
  return (api.on as jest.Mock).mock.calls.filter((c) => c[0] === "before_agent_start").map((c) => c[1] as Handler);
}

describe("memory steer", () => {
  it("appends the default steer as system context on a real turn", async () => {
    const { api } = createPluginApi(plugin);
    const [h] = steerHandlers(api);
    expect(h).toBeDefined();
    const out = (await h({ prompt: "what did we decide?" }, { agentId: "will", trigger: "user" })) as { appendSystemContext: string };
    expect(out.appendSystemContext).toBe(DEFAULT_MEMORY_STEER_TEXT);
    expect(out.appendSystemContext).toMatch(/memory_search \/ memory_get/);
    expect(out.appendSystemContext).toMatch(/memory_forget/);
  });

  it("stays silent on harness-noise turns", async () => {
    const { api } = createPluginApi(plugin);
    const [h] = steerHandlers(api);
    expect(await h({ prompt: "Read HEARTBEAT.md if it exists" }, { agentId: "will" })).toBeUndefined();
    expect(await h({ prompt: "anything" }, { agentId: "will", trigger: "cron" })).toBeUndefined();
  });

  it("honours a text override and the off-switch", async () => {
    const custom = createPluginApi(plugin, { memorySteerText: "Use Cognee." });
    const out = (await steerHandlers(custom.api)[0]({ prompt: "hi there" }, {})) as { appendSystemContext: string };
    expect(out.appendSystemContext).toBe("Use Cognee.");

    const off = createPluginApi(plugin, { memorySteer: false });
    expect(steerHandlers(off.api)).toHaveLength(0);
  });
});

describe("cognee version / status version line", () => {
  let logSpy: jest.SpyInstance;
  let exitSpy: jest.SpyInstance;
  beforeEach(() => {
    logSpy = jest.spyOn(console, "log").mockImplementation(() => {});
    exitSpy = jest.spyOn(process, "exit").mockImplementation(((code?: number) => { throw new Error(`process.exit(${code})`); }) as never);
  });
  afterEach(() => { logSpy.mockRestore(); exitSpy.mockRestore(); });

  function lines(): string[] {
    return logSpy.mock.calls.map((c) => String(c[0]));
  }

  it("prints the installed version, preferring api.version, with no hint when the cache is empty", async () => {
    const harness = createPluginApi(plugin);
    (harness.api as { version?: string }).version = undefined;
    await expect(harness.runCli("version")).rejects.toThrow(/process\.exit\(0\)/);
    expect(lines()[0]).toMatch(/^Plugin: cognee-openclaw v\d{4}\.\d+\.\d+$/);
    expect(lines().some((l) => l.startsWith("Update available"))).toBe(false);
  });

  it("prints an update hint when the cached latest is newer than the running version", async () => {
    writeFileSync(updateCachePath, JSON.stringify({ checkedAt: Date.now(), latest: "2999.1.1" }));
    const harness = createPluginApi(plugin);
    await expect(harness.runCli("version")).rejects.toThrow(/process\.exit\(0\)/);
    expect(lines()[1]).toBe("Update available: v2999.1.1. Run: openclaw plugins install @cognee/cognee-openclaw@latest");
  });

  it("prints no hint when the cached latest is not newer", async () => {
    writeFileSync(updateCachePath, JSON.stringify({ checkedAt: Date.now(), latest: "2000.1.1" }));
    const harness = createPluginApi(plugin);
    await expect(harness.runCli("version")).rejects.toThrow(/process\.exit\(0\)/);
    expect(lines()).toHaveLength(1);
  });

  it("status leads with the version line", async () => {
    const harness = createPluginApi(plugin);
    await expect(harness.runCli("status")).rejects.toThrow(/process\.exit\(0\)/);
    expect(lines()[0]).toMatch(/^Plugin: cognee-openclaw v/);
  });
});
