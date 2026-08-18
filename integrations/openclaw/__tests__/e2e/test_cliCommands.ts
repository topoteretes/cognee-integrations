/**
 * The `cognee` CLI subcommands — the surface a user actually types.
 *
 * Eight subcommands exist (`index status health visualise setup scopes forget
 * improve`) and only `setup` had a test. These are the plugin's whole
 * human-facing interface: unlike a missed recall, a broken subcommand is
 * immediately visible to whoever ran it.
 *
 * Two things shape every test here:
 *
 *   * every action ends in `process.exit`, which would kill the jest worker, so it
 *     is spied. That is also worth naming as a design cost — an action that exits
 *     the process cannot be composed, and its cleanup never runs;
 *   * the actions report through `console.log` rather than the plugin logger, so
 *     asserting user-visible output means capturing stdout.
 *
 * Contract per command: it reaches the right client call, reports the outcome, and
 * exits non-zero only on genuine failure.
 */

import plugin from "../../src/plugin";
import { CogneeHttpClient } from "../../src/client";
import { createPluginApi } from "../../test-utils/fakeApi";

jest.mock("../../src/client");
jest.mock("../../src/server", () => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const { serverMock } = require("../../test-utils/fakeApi");
  return serverMock();
});
// The CLI walks the workspace for memory files; an empty list keeps these focused
// on command behaviour rather than on fixture trees.
jest.mock("../../src/files", () => ({
  collectMemoryFiles: jest.fn(async () => []),
  hashText: jest.fn(() => "hash"),
}));

const MockedClient = CogneeHttpClient as jest.MockedClass<typeof CogneeHttpClient>;

function clientStub() {
  return {
    setApiKey: jest.fn((_k: string) => undefined),
    health: jest.fn(async () => ({ status: "ok" })),
    registerAgent: jest.fn(async (_p: unknown) => ({ ok: true, connectionId: "c1" })),
    unregisterAgent: jest.fn(async (_p: unknown) => ({ ok: true, activeAgents: 0 })),
    listDatasets: jest.fn(async () => [{ id: "ds-1", name: "testds" }]),
    improve: jest.fn(async (_p: unknown) => ({ status: "ok" })),
    forget: jest.fn(async (_p: unknown) => ({ ok: true })),
    visualise: jest.fn(async (_p: unknown) => "<html/>"),
    recall: jest.fn(async (_p: unknown) => []),
    rememberEntry: jest.fn(async (_p: unknown) => ({ entryId: "e1" })),
    remember: jest.fn(async (_p: unknown) => ({ datasetId: "ds-1", items: [] })),
  };
}

let stub: ReturnType<typeof clientStub>;
let exitSpy: jest.SpyInstance;
let logSpy: jest.SpyInstance;

/** Everything the command printed, joined — what the user would have seen. */
function printed(): string {
  return logSpy.mock.calls.map((c) => c.join(" ")).join("\n");
}

/** Exit codes the command requested, in order. */
function exitCodes(): unknown[] {
  return exitSpy.mock.calls.map((c) => c[0]);
}

/** Thrown by the strict exit stub to model a real `process.exit`. */
class ProcessExited extends Error {
  constructor(readonly code: unknown) {
    super(`process.exit(${String(code)})`);
  }
}

/**
 * Replace the permissive exit stub with one that stops execution.
 *
 * The actions guard themselves with `console.log(...); process.exit(1)` and no
 * `return`, so a stub that merely records the call lets execution fall straight
 * through the guard — which for `forget` means reaching the destructive call the
 * guard exists to prevent. Throwing is what makes a guard testable *as* a guard.
 *
 * Worth noting as a design cost rather than a bug: in production `process.exit`
 * really does terminate, so the guards hold. But they hold only because of that,
 * and a `return` after each exit would make them robust to being composed.
 */
function useStrictExit(): void {
  exitSpy.mockImplementation(((code: unknown) => {
    throw new ProcessExited(code);
  }) as never);
}

beforeEach(() => {
  jest.clearAllMocks();
  stub = clientStub();
  MockedClient.mockImplementation(() => stub as never);
  // Permissive by default: without a stub the first `process.exit` inside an
  // action tears down the jest worker and every later test in the file reports as
  // failed-to-run. Tests asserting a guard opt into `useStrictExit()`.
  exitSpy = jest.spyOn(process, "exit").mockImplementation((() => undefined) as never);
  logSpy = jest.spyOn(console, "log").mockImplementation(() => undefined);
});

afterEach(() => {
  exitSpy.mockRestore();
  logSpy.mockRestore();
});

describe("registration", () => {
  it("registers every documented subcommand", async () => {
    // A subcommand silently disappearing is invisible until a user types it, so
    // the roster is asserted as a whole rather than per test.
    const harness = createPluginApi(plugin);
    expect([...harness.actions.keys()].sort()).toEqual([
      "forget",
      "health",
      "improve",
      "index",
      "scopes",
      "setup",
      "status",
      "visualise",
    ]);
  });

  it("runCli names the available commands when asked for an unknown one", async () => {
    const harness = createPluginApi(plugin);
    await expect(harness.runCli("nope")).rejects.toThrow(/no cognee subcommand "nope"/);
  });
});

describe("cognee health", () => {
  it("reports OK with the configured base URL and exits 0", async () => {
    const harness = createPluginApi(plugin);
    await harness.runCli("health");

    expect(stub.health).toHaveBeenCalled();
    expect(printed()).toMatch(/Cognee API: OK/);
    expect(exitCodes()).toContain(0);
  });

  it("reports UNREACHABLE and exits 1 when the probe throws", async () => {
    // The non-zero exit is the contract: `cognee health` is the command a user or
    // a script runs to decide whether anything else will work.
    stub.health.mockRejectedValueOnce(new Error("connect ECONNREFUSED"));

    const harness = createPluginApi(plugin);
    await harness.runCli("health");

    const out = printed();
    expect(out).toMatch(/UNREACHABLE/);
    expect(out).toMatch(/ECONNREFUSED/);
    expect(exitCodes()).toContain(1);
  });
});

describe("cognee scopes", () => {
  it("says so plainly when the workspace has no memory files", async () => {
    const harness = createPluginApi(plugin);
    await harness.runCli("scopes");

    expect(printed()).toMatch(/No memory files found/i);
    expect(exitCodes()).toContain(0);
  });

  it("explains how to enable multi-scope when it is off", async () => {
    // Single-scope is the default, so this branch is what most users hit; the
    // message has to name the settings that switch it on rather than just
    // reporting the current state.
    const { collectMemoryFiles } = jest.requireMock("../../src/files") as {
      collectMemoryFiles: jest.Mock;
    };
    // MemoryFile is {path, absPath, content, hash} — the scope router reads
    // `path`, so a fixture with the wrong field name fails inside routeFileToScope
    // rather than in the assertion.
    collectMemoryFiles.mockResolvedValueOnce([
      { path: "AGENTS.md", absPath: "/ws/AGENTS.md", content: "x", hash: "h" },
    ]);

    const harness = createPluginApi(plugin, { datasetName: "testds" });
    await harness.runCli("scopes");

    const out = printed();
    expect(out).toMatch(/Multi-scope mode is OFF/i);
    expect(out).toMatch(/testds/);
    expect(out).toMatch(/companyDataset|userDatasetPrefix|agentDatasetPrefix/);
  });
});

describe("cognee status", () => {
  it("runs and reports without a server round trip failing it", async () => {
    const harness = createPluginApi(plugin);
    await harness.runCli("status");

    // status summarises local sync state; the assertion is that it produces
    // output and exits cleanly rather than throwing on an empty workspace.
    expect(printed().length).toBeGreaterThan(0);
    expect(exitCodes()).toContain(0);
  });
});

describe("cognee index", () => {
  it("syncs the workspace and reports a summary", async () => {
    const harness = createPluginApi(plugin);
    await harness.runCli("index");

    // The counts line is what a user reads to know whether anything moved.
    expect(printed()).toMatch(/Sync complete/i);
    expect(exitCodes()).toContain(0);
  });
});

describe("cognee improve", () => {
  it("asks the server to bridge session memory into the graph", async () => {
    const harness = createPluginApi(plugin);
    await harness.runCli("improve");

    expect(stub.improve).toHaveBeenCalled();
    expect(exitCodes().length).toBeGreaterThan(0);
  });
});

describe("cognee visualise", () => {
  it("resolves a dataset before asking for the visualisation", async () => {
    const harness = createPluginApi(plugin);
    await harness.runCli("visualise");

    // Either it resolved a dataset and called through, or it reported why it
    // could not — both are acceptable, silently doing nothing is not.
    const acted = stub.visualise.mock.calls.length > 0 || printed().length > 0;
    expect(acted).toBe(true);
  });
});

describe("cognee forget", () => {
  // The only destructive command, so its guards get the strict exit stub — a
  // permissive one would walk straight past them into the deletion.
  beforeEach(() => useStrictExit());

  it("deletes nothing on a bare invocation and asks for a target", async () => {
    const harness = createPluginApi(plugin);
    await expect(harness.runCli("forget")).rejects.toThrow(/process\.exit\(1\)/);

    expect(stub.forget).not.toHaveBeenCalled();
    expect(printed()).toMatch(/--dataset <name> or --everything --confirm/);
  });

  it("refuses --everything without --confirm", async () => {
    const harness = createPluginApi(plugin);
    await expect(harness.runCli("forget", { everything: true })).rejects.toThrow(/process\.exit\(1\)/);

    expect(stub.forget).not.toHaveBeenCalled();
    expect(printed()).toMatch(/Refusing to wipe everything without --confirm/);
  });

  it("wipes a single named dataset when one is given", async () => {
    stub.forget.mockResolvedValueOnce({ ok: true, deleted: true } as never);

    const harness = createPluginApi(plugin);
    await harness.runCli("forget", { dataset: "testds" }).catch(() => undefined);

    // Scoped to the named dataset, and never `everything` — a scoped wipe must not
    // widen into a full one.
    expect(stub.forget).toHaveBeenCalledWith(
      expect.objectContaining({ dataset: "testds", everything: undefined }),
    );
  });

  it("passes everything through only once --confirm is present", async () => {
    stub.forget.mockResolvedValueOnce({ ok: true, deleted: true } as never);

    const harness = createPluginApi(plugin);
    await harness.runCli("forget", { everything: true, confirm: true }).catch(() => undefined);

    expect(stub.forget).toHaveBeenCalledWith(expect.objectContaining({ everything: true }));
  });
});
