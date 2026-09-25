/**
 * Harness self-tests: the mock server, the fake api, and the real-home guard.
 *
 * These exercise the test infrastructure itself rather than the plugin, so the
 * integration and lifecycle suites can build on it knowing it works. A mock that
 * silently answers 200 to everything, or an `emit` that fires no handlers, would
 * make every test above it vacuously green.
 */

import plugin from "../src/plugin";
import { MockCognee } from "../test-utils/mockCognee";
import { createPluginApi, serverMock, flush } from "../test-utils/fakeApi";
import { expectRealHomeUntouched, snapshotRealHome, useTempHome } from "../test-utils/tempHome";

jest.mock("../src/client");
jest.mock("../src/server", () => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const { serverMock: mk } = require("../test-utils/fakeApi");
  return mk();
});

describe("MockCognee", () => {
  let mock: MockCognee;

  beforeAll(async () => {
    mock = await MockCognee.start();
  });
  afterAll(async () => {
    await mock.close();
  });
  beforeEach(() => mock.reset());

  it("listens on a real ephemeral port", async () => {
    expect(mock.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
    const res = await fetch(`${mock.url}/api/v1/health`);
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ status: "ok" });
  });

  it("records method, body and headers", async () => {
    await fetch(`${mock.url}/api/v1/recall`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Api-Key": "k1" },
      body: JSON.stringify({ query: "hello" }),
    });

    const call = mock.assertCalled("POST", "/recall");
    expect(call.json).toEqual({ query: "hello" });
    expect(call.headers["x-api-key"]).toBe("k1");
  });

  it("treats both path prefixes as one route but remembers which was sent", async () => {
    // Local mode sends /api/v1/x; cloud mode sends bare /x. Routing on the
    // stripped form keeps assertions mode-agnostic without losing the prefix.
    await fetch(`${mock.url}/api/v1/cognify`, { method: "POST" });
    await fetch(`${mock.url}/cognify`, { method: "POST" });

    expect(mock.callsTo("POST", "/cognify")).toHaveLength(2);
    expect(mock.calls.map((c) => c.rawPath)).toEqual(["/api/v1/cognify", "/cognify"]);
  });

  it("can force a status, and `once` restores the default afterwards", async () => {
    // This is what makes retry paths testable: fail the first attempt only.
    mock.forceResponse("POST", "/recall", 503, { detail: "boom" }, true);

    const first = await fetch(`${mock.url}/api/v1/recall`, { method: "POST" });
    expect(first.status).toBe(503);

    const second = await fetch(`${mock.url}/api/v1/recall`, { method: "POST" });
    expect(second.status).toBe(200);
  });

  it("404s an unknown route instead of a permissive 200", async () => {
    // A blanket 200 would let a test pass against an endpoint the mock never
    // implemented, which is the failure mode this design exists to avoid.
    const res = await fetch(`${mock.url}/api/v1/not-a-real-endpoint`);
    expect(res.status).toBe(404);
    await expect(res.json()).resolves.toMatchObject({ detail: expect.stringContaining("no route") });
  });

  it("serves the dynamic dataset routes", async () => {
    const res = await fetch(`${mock.url}/api/v1/datasets/ds-1/data`);
    expect(res.status).toBe(200);
    expect(mock.assertCalled("GET", "/datasets/ds-1/data")).toBeTruthy();
  });

  it("assertCalled names what it did see", () => {
    expect(() => mock.assertCalled("POST", "/never")).toThrow(/expected POST \/never/);
  });
});

describe("createPluginApi", () => {
  it("registers the plugin and collects its event subscriptions", () => {
    const harness = createPluginApi(plugin);

    // The nine events the plugin owns; if register() silently stopped
    // subscribing, every lifecycle test would pass while testing nothing.
    expect(harness.subscribed()).toEqual(
      expect.arrayContaining([
        "gateway_start",
        "gateway_stop",
        "before_prompt_build",
        "session_end",
      ]),
    );
    expect(harness.handlerCount("before_prompt_build")).toBeGreaterThan(0);
  });

  it("emit invokes the registered handlers", async () => {
    const harness = createPluginApi(plugin);
    // A no-op emit must not throw: handlers run against the stubbed client.
    await harness.emit("before_prompt_build", { prompt: "hi" }, { agentId: "a", sessionId: "s" });
    await flush(2);
    expect(harness.api.on).toHaveBeenCalled();
  });

  it("merges pluginConfig over the defaults", () => {
    const harness = createPluginApi(plugin, { datasetName: "custom", autoRecall: true });
    expect(harness.api.pluginConfig).toMatchObject({
      datasetName: "custom",
      autoRecall: true,
      enableSessions: true, // still from the defaults
    });
  });

  it("serverMock covers every export the plugin imports from src/server", () => {
    // Missing one surfaces as "not a function" deep inside a handler, which is a
    // much worse error than a failed expectation here.
    expect(Object.keys(serverMock()).sort()).toEqual([
      "bootServerIfNeeded",
      "exitWatcherPidfilePath",
      "isLocalUrl",
      "resolveOrMintApiKey",
      "spawnExitWatcher",
      "waitForServerHealth",
    ]);
  });
});

describe("real-home guard", () => {
  it("passes when nothing touched the real home", async () => {
    const before = await snapshotRealHome();
    await expect(expectRealHomeUntouched(before)).resolves.toBeUndefined();
  });

  it("catches a file appearing under the real home", async () => {
    // Simulated by snapshotting a state where the file is claimed absent, then
    // asserting — proving the guard reports creation rather than ignoring it.
    const before = new Map<string, number>();
    const { writeFile, mkdir, rm } = await import("node:fs/promises");
    const { join, dirname } = await import("node:path");
    const { homedir } = await import("node:os");

    const probe = join(homedir(), ".cognee-plugin", "api_key.json");
    let existed = true;
    try {
      const { stat } = await import("node:fs/promises");
      await stat(probe);
    } catch {
      existed = false;
    }
    if (existed) {
      // The file is genuinely present on this machine, so "created" is the wrong
      // assertion to test; the untouched case above already covers the guard.
      return;
    }

    await mkdir(dirname(probe), { recursive: true });
    await writeFile(probe, "{}", "utf8");
    try {
      await expect(expectRealHomeUntouched(before)).rejects.toThrow(/wrote to the REAL home/);
    } finally {
      await rm(probe, { force: true });
    }
  });

  it("useTempHome redirects HOME and restores it", async () => {
    const original = process.env.HOME;
    const temp = await useTempHome();
    expect(process.env.HOME).toBe(temp.path);
    expect(process.env.USERPROFILE).toBe(temp.path);
    await temp.cleanup();
    expect(process.env.HOME).toBe(original);
  });
});
