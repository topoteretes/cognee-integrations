import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import plugin from "../../src/plugin";
import { CogneeHttpClient } from "../../src/client";
import { createPluginApi, flush } from "../../test-utils/fakeApi";
import { loadAgentSyncIndexes } from "../../src/persistence";

jest.mock("../../src/client");
jest.mock("../../src/version", () => ({
  ...jest.requireActual("../../src/version"),
  runUpdateCheck: jest.fn(async () => {}),
}));
jest.mock("../../src/server", () => require("../../test-utils/fakeApi").serverMock());
jest.mock("openclaw/plugin-sdk/agent-runtime", () => ({
  listAgentIds: jest.fn(),
  resolveAgentWorkspaceDir: jest.fn(),
}));

const MockedClient = CogneeHttpClient as jest.MockedClass<typeof CogneeHttpClient>;
const { listAgentIds: hostIds, resolveAgentWorkspaceDir: hostWorkspace } =
  jest.requireMock("openclaw/plugin-sdk/agent-runtime") as Record<string, jest.Mock>;
type Remember = Parameters<CogneeHttpClient["remember"]>[0];
type Recall = Parameters<CogneeHttpClient["recall"]>[0];
class CliExit extends Error {
  constructor(readonly code: number) { super(`exit ${code}`); }
}

async function runCli(h: ReturnType<typeof createPluginApi>, name: string, opts = {}) {
  try { await h.runCli(name, opts); }
  catch (error) { if (!(error instanceof CliExit) || error.code !== 0) throw error; }
}

it("resolves configured workspaces through the installed host SDK's public exports", () => {
  const workspace = join(tmpdir(), "native-sdk-workspace");
  const config = { agents: { list: [{ id: "native-sdk-agent", workspace }] } };
  // Use native ESM resolution rather than Jest's mapped SDK mock.
  const output = execFileSync(process.execPath, ["--input-type=module", "-e", `
    const sdk = await import("openclaw/plugin-sdk/agent-runtime");
    const config = ${JSON.stringify(config)};
    console.log(JSON.stringify(sdk.listAgentIds(config).map(id => ({
      id, workspace: sdk.resolveAgentWorkspaceDir(config, id)
    }))));
    process.exit(0);
  `], { cwd: join(__dirname, "../.."), encoding: "utf8", timeout: 30_000 });
  expect(JSON.parse(output)).toEqual([{ id: "native-sdk-agent", workspace }]);
}, 35_000);

describe.each(["entries", "list"] as const)("per-agent sync with agents.%s", (rosterKind) => {
  let gateway: string;
  let red: string;
  let blue: string;
  let runtimeConfig: Record<string, unknown>;
  let stub: ReturnType<typeof makeClient>;
  let exitSpy: jest.SpyInstance;
  let logSpy: jest.SpyInstance;

  function makeClient() {
    const stored = new Map<string, string>();
    return {
      setApiKey: jest.fn(),
      health: jest.fn(async () => ({ status: "ok" })),
      remember: jest.fn(async (p: Remember) => {
        stored.set(p.datasetName, p.files.map((f) => f.data).join("\n"));
        return {
          datasetId: `ds-${p.datasetName}`,
          items: p.files.map((f) => ({ filePath: f.filePath, dataId: `${p.datasetName}/${f.filePath}` })),
        };
      }),
      update: jest.fn(),
      forget: jest.fn(async () => ({ deleted: true })),
      registerAgent: jest.fn(async () => ({ connectionId: "connection" })),
      unregisterAgent: jest.fn(async () => ({ activeAgents: 1 })),
      listDatasets: jest.fn(async () => [...stored.keys()].map((name) => ({ name, id: `ds-${name}` }))),
      recall: jest.fn(async (p: Recall) => p.datasetIds.flatMap((id) => {
        const text = stored.get(id.replace(/^ds-/, ""));
        return text ? [{ id, text, score: 1 }] : [];
      })),
    };
  }

  function harness(registerCli = true, cliWorkspace = gateway, agentId = "main") {
    return createPluginApi(plugin, {
      datasetName: "company",
      companyDataset: "company",
      agentDatasetPrefix: "agent",
      agentId,
      perAgentMemory: true,
      autoIndex: true,
      autoCognify: false,
      enableSessions: false,
      captureSession: false,
      persistSessionsAfterEnd: false,
      improveOnSessionEnd: false,
      recallScopes: ["company", "agent"],
    }, { workspaceDir: cliWorkspace, runtimeConfig, registerCli });
  }

  beforeEach(async () => {
    jest.clearAllMocks();
    // Every test gets independent real workspace files and real on-disk indexes.
    const root = await mkdtemp(join(tmpdir(), "cognee-agent-workspaces-"));
    gateway = join(root, "gateway");
    red = join(root, "red");
    blue = join(root, "blue");
    await Promise.all([gateway, red, blue].map((ws) => mkdir(ws, { recursive: true })));
    await mkdir(join(gateway, "memory", "company"), { recursive: true });
    await writeFile(join(gateway, "MEMORY.md"), "GATEWAY_PRIVATE");
    await writeFile(join(red, "MEMORY.md"), "RED_PRIVATE");
    await writeFile(join(blue, "MEMORY.md"), "BLUE_PRIVATE");
    await writeFile(join(gateway, "memory", "company", "policy.md"), "SHARED_POLICY");
    // A secondary agent's local company folder must not replace the shared index.
    await mkdir(join(red, "memory", "company"), { recursive: true });
    await writeFile(join(red, "memory", "company", "policy.md"), "WRONG_COMPANY_ROOT");
    const entries = [{ id: "main", workspace: gateway }, { id: "red", workspace: red }, { id: "blue", workspace: blue }];
    runtimeConfig = {
      agents: {
        defaults: { workspace: gateway },
        ...(rosterKind === "entries"
          ? { entries: Object.fromEntries(entries.map(({ id, ...entry }) => [id, entry])) }
          : { list: entries }),
      },
    };
    // The host owns roster compatibility and default workspace derivation. The
    // plugin must use its public SDK instead of re-parsing either config shape.
    hostIds.mockReturnValue(entries.map(({ id }) => id));
    hostWorkspace.mockImplementation((_config, id) => ({ main: gateway, red, blue })[id]);
    stub = makeClient();
    MockedClient.mockImplementation(() => stub as never);
    exitSpy = jest.spyOn(process, "exit").mockImplementation(((code: number) => { throw new CliExit(code); }) as never);
    logSpy = jest.spyOn(console, "log").mockImplementation(() => undefined);
    const stateDir = join(homedir(), ".openclaw", "memory", "cognee");
    await mkdir(stateDir, { recursive: true });
    for (const file of ["datasets.json", "scoped-sync-indexes.json", "agent-sync-indexes.json", "sync-index.json"]) {
      await writeFile(join(stateDir, file), "{}");
    }
  });

  afterEach(() => {
    exitSpy.mockRestore();
    logSpy.mockRestore();
  });

  it("preserves the plugin agent and gateway workspace when no roster is configured", async () => {
    runtimeConfig = {};
    hostIds.mockReturnValue(["main"]);
    const h = harness(false, gateway, "custom");
    await h.emit("gateway_start", {}, { workspaceDir: gateway });
    for (let i = 0; i < 100 && stub.remember.mock.calls.length < 2; i++) await flush(1);
    expect(uploads()).toEqual({
      company: expect.stringContaining("SHARED_POLICY"),
      "agent-custom": expect.stringContaining("GATEWAY_PRIVATE"),
    });
    expect(hostWorkspace).not.toHaveBeenCalled();
  });

  function uploads() {
    return Object.fromEntries(stub.remember.mock.calls.map(([p]) => [p.datasetName, p.files.map((f) => f.data).join("\n")]));
  }

  it("seeds every configured workspace at gateway startup without sharing private files", async () => {
    const h = harness(false);
    await h.emit("gateway_start", {}, { workspaceDir: gateway });
    for (let i = 0; i < 100 && !h.logger.info.mock.calls.some(([s]) => String(s).includes('seeded agent "blue"')); i++) await flush(1);
    expect(uploads()).toEqual({
      company: expect.stringContaining("SHARED_POLICY"),
      "agent-main": expect.stringContaining("GATEWAY_PRIVATE"),
      "agent-red": expect.stringContaining("RED_PRIVATE"),
      "agent-blue": expect.stringContaining("BLUE_PRIVATE"),
    });
    expect(JSON.stringify(uploads())).not.toContain("WRONG_COMPANY_ROOT");
    expect(stub.forget).not.toHaveBeenCalled();
  });

  it("uses configured agent workspaces for post-turn and session-end sweeps", async () => {
    const h = harness();
    await h.emit("gateway_start", {}, { workspaceDir: gateway });
    // A session's task cwd can differ from the agent's authoritative workspace.
    await h.emit("agent_end", { success: true }, { agentId: "red", sessionId: "r", workspaceDir: blue });
    await h.emit("agent_end", { success: true }, { agentId: "blue", sessionId: "b", workspaceDir: red });
    expect(uploads()["agent-red"]).toContain("RED_PRIVATE");
    expect(uploads()["agent-red"]).not.toContain("BLUE_PRIVATE");
    expect(uploads()["agent-blue"]).toContain("BLUE_PRIVATE");
    stub.remember.mockClear();
    await writeFile(join(red, "memory", "late.md"), "RED_LATE_NOTE");
    await h.emit("session_end", {}, { agentId: "red", sessionId: "r" });
    for (let i = 0; i < 100 && stub.remember.mock.calls.length === 0; i++) await flush(1);
    expect(uploads()).toEqual({ "agent-red": expect.stringContaining("RED_LATE_NOTE") });
    expect(stub.forget).not.toHaveBeenCalled();
  });

  it("indexes the selected agent from any CLI cwd and enumerates unindexed agents in status", async () => {
    const h = harness(true, blue);
    await runCli(h, "status");
    expect(logSpy.mock.calls.flat().join("\n")).toContain("[AGENT:red]");
    await runCli(h, "index", { agent: "red" });
    expect(uploads()).toEqual({ "agent-red": expect.stringContaining("RED_PRIVATE") });
    expect(Object.keys(await loadAgentSyncIndexes())).toEqual(["red"]);
  });

  it("delegates omitted workspace defaults to the host instead of reusing the gateway folder", async () => {
    // Native OpenClaw can derive secondary workspaces without an explicit path.
    hostIds.mockReturnValue(["main", "red", "blue"]);
    const agents = runtimeConfig.agents as Record<string, unknown>;
    if (rosterKind === "entries") agents.entries = { main: {}, red: {}, blue: {} };
    else agents.list = [{ id: "main" }, { id: "red" }, { id: "blue" }];
    await runCli(harness(), "index", { agent: "red" });
    expect(uploads()).toEqual({ "agent-red": expect.stringContaining("RED_PRIVATE") });
    expect(hostWorkspace).toHaveBeenCalledWith(runtimeConfig, "red");
  });

  it("recalls the shared company graph plus only the calling agent's graph", async () => {
    const h = harness();
    await runCli(h, "index");
    for (const [id, own, other] of [["red", "RED_PRIVATE", "BLUE_PRIVATE"], ["blue", "BLUE_PRIVATE", "RED_PRIVATE"]]) {
      const tool = h.tools({ agentId: id, sessionId: id }).find((t) => t.name === "memory_search")!;
      const result = await tool.execute("recall", { query: "notes and policy" });
      expect(JSON.stringify(result)).toContain("SHARED_POLICY");
      expect(JSON.stringify(result)).toContain(own);
      expect(JSON.stringify(result)).not.toContain(other);
      expect(JSON.stringify(result)).not.toContain("GATEWAY_PRIVATE");
    }
  });

  it("does not sync the gateway folder when host workspace resolution fails", async () => {
    const h = harness();
    await h.emit("gateway_start", {}, { workspaceDir: gateway });
    hostWorkspace.mockImplementation(() => { throw new Error("workspace unavailable"); });
    await h.emit("agent_end", { success: true }, { agentId: "red", sessionId: "r", workspaceDir: gateway });
    await h.emit("session_end", {}, { agentId: "red", sessionId: "r" });
    await flush(20);
    expect(stub.remember).not.toHaveBeenCalled();
    expect(stub.forget).not.toHaveBeenCalled();
    expect(h.logger.warn.mock.calls.flat().join("\n")).toContain("workspace unavailable");
    // Failed final sweeps must release the session fence so they can be retried.
    hostWorkspace.mockImplementation((_config, id) => ({ main: gateway, red, blue })[id]);
    await h.emit("session_end", {}, { agentId: "red", sessionId: "r" });
    for (let i = 0; i < 100 && stub.remember.mock.calls.length === 0; i++) await flush(1);
    expect(uploads()).toEqual({ "agent-red": expect.stringContaining("RED_PRIVATE") });
  });
});
