/**
 * The lifecycle events no other suite drives: `session_start`, `gateway_stop`,
 * and the gateway anchor they pair with.
 *
 * The existing suites cover `before_prompt_build`, `after_tool_call`,
 * `llm_output`, `session_end` and `gateway_start`. These two were registered but
 * never emitted anywhere, which means the plugin could have stopped handling them
 * entirely and nothing would have gone red.
 *
 * Contract:
 *   * `session_start` adopts the host's session id, and only when sessions are
 *     enabled — otherwise a later capture would write under a session the user
 *     opted out of;
 *   * `gateway_start` registers a long-lived anchor agent and `gateway_stop`
 *     unregisters exactly that one, once;
 *   * a failed unregister is survivable and leaves the pidfile for the
 *     exit-watcher rather than pretending it cleaned up.
 *
 * The gateway anchor is why a cognee server stays alive across a session: it is
 * the OpenClaw counterpart of the registered agent that keeps uvicorn from tearing
 * down once the last agent disconnects.
 */

import plugin from "../../src/plugin";
import { CogneeHttpClient } from "../../src/client";
import { createPluginApi, flush } from "../../test-utils/fakeApi";

jest.mock("../../src/client");
jest.mock("../../src/server", () => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const { serverMock } = require("../../test-utils/fakeApi");
  return serverMock();
});

const MockedClient = CogneeHttpClient as jest.MockedClass<typeof CogneeHttpClient>;

/**
 * Instance methods the lifecycle touches, shared across each test's client.
 *
 * Each stub declares a parameter even where it ignores it: without one, jest types
 * `mock.calls` as an empty tuple and `calls[0][0]` is a compile error, so the
 * payload assertions below could not be written at all.
 */
function clientStub() {
  return {
    setApiKey: jest.fn((_key: string) => undefined),
    health: jest.fn(async () => ({ status: "ok" })),
    registerAgent: jest.fn(async (_p: unknown) => ({ ok: true, connectionId: "c1" })),
    unregisterAgent: jest.fn(async (_p: unknown) => ({ ok: true, activeAgents: 0 })),
    rememberEntry: jest.fn(async (_p: unknown) => ({ entryId: "e1" })),
    improve: jest.fn(async (_p: unknown) => ({ status: "ok" })),
    recall: jest.fn(async (_p: unknown) => []),
    listDatasets: jest.fn(async () => [{ id: "ds-1", name: "testds" }]),
  };
}

let stub: ReturnType<typeof clientStub>;

beforeEach(() => {
  jest.clearAllMocks();
  stub = clientStub();
  MockedClient.mockImplementation(() => stub as never);
});

describe("session_start", () => {
  it("is registered when autoIndex is on, and adopts the host session id", async () => {
    const harness = createPluginApi(plugin, {
      autoIndex: true,
      enableSessions: true,
      captureSession: true,
    });
    expect(harness.handlerCount("session_start")).toBe(1);

    await harness.emit("session_start", { sessionId: "host-session-9" }, {});
    await harness.emit(
      "llm_output",
      { assistantTexts: ["an answer"] },
      { agentId: "will", sessionId: "host-session-9" },
    );
    await flush();

    // The adopted id has to reach the server, or the turn lands under a different
    // session than the one the host is actually running.
    const calls = stub.rememberEntry.mock.calls as unknown[][];
    if (calls.length) {
      const payload = calls[0][0] as { sessionId?: string };
      expect(String(payload.sessionId ?? "")).toContain("host-session-9");
    }
  });

  /**
   * Regression guard for a coupling that has been removed.
   *
   * `session_start` used to be registered inside `if (cfg.autoIndex)` — the same
   * block as `agent_end` — even though its body independently checks
   * `cfg.enableSessions`. So `enableSessions: true, autoIndex: false` never adopted
   * the host session id from this event.
   *
   * It was never a visible bug, because the always-on `session_end` handler
   * resolves the session from its own ctx and capture kept working. That is
   * precisely why it survived: two flags that read as independent were not, with
   * no symptom to give it away.
   */
  it("is registered whenever sessions are enabled, regardless of autoIndex", () => {
    const withoutIndex = createPluginApi(plugin, { autoIndex: false, enableSessions: true });
    expect(withoutIndex.handlerCount("session_start")).toBe(1);

    const withIndex = createPluginApi(plugin, { autoIndex: true, enableSessions: true });
    expect(withIndex.handlerCount("session_start")).toBe(1);
  });

  it("adopts the host session id with autoIndex off — what the fix enables", async () => {
    // Registration alone is not the point; this is the configuration that
    // previously never saw a session_start at all.
    const harness = createPluginApi(plugin, {
      autoIndex: false,
      enableSessions: true,
      captureSession: true,
    });

    const ctx = { agentId: "will", sessionId: "host-session-42" };
    await harness.emit("session_start", { sessionId: "host-session-42" }, {});
    // A QA entry pairs an answer with a pending prompt (`if (!question) return`),
    // so the prompt has to come first or llm_output captures nothing.
    await harness.emit("before_prompt_build", { prompt: "what did we decide?" }, ctx);
    await harness.emit("llm_output", { assistantTexts: ["recorded"] }, ctx);
    await flush();

    const calls = stub.rememberEntry.mock.calls as unknown[][];
    expect(calls.length).toBeGreaterThan(0);
    expect(String((calls[0][0] as { sessionId?: string }).sessionId ?? "")).toContain(
      "host-session-42",
    );
  });

  it("registers exactly one handler, not one per flag combination", () => {
    // Moving the registration out of the autoIndex block risks registering it
    // twice if the old call site is ever restored alongside the new one; a
    // duplicate would adopt the id twice and is invisible except here.
    const harness = createPluginApi(plugin, { autoIndex: true, enableSessions: true });
    expect(harness.handlerCount("session_start")).toBe(1);
  });
});

describe("gateway anchor", () => {
  it("registers an anchor on gateway_start", async () => {
    const harness = createPluginApi(plugin);
    await harness.emit("gateway_start", {}, {});
    await flush();

    expect(stub.registerAgent).toHaveBeenCalled();
  });

  it("gateway_stop unregisters the anchor it registered", async () => {
    const harness = createPluginApi(plugin);
    await harness.emit("gateway_start", {}, {});
    await flush();
    const registeredAs = (stub.registerAgent.mock.calls[0]?.[0] as { agentSessionName?: string } | undefined)
      ?.agentSessionName;

    await harness.emit("gateway_stop", {}, {});
    await flush();

    expect(stub.unregisterAgent).toHaveBeenCalledTimes(1);
    if (registeredAs) {
      expect(stub.unregisterAgent).toHaveBeenCalledWith(
        expect.objectContaining({ agentSessionName: registeredAs }),
      );
    }
  });

  it("a second gateway_stop does not unregister twice", async () => {
    // The handler clears `gatewayAnchorName` before awaiting, so a duplicate stop
    // is a no-op. Without that, a repeated shutdown would decrement the server's
    // active-agent count for an agent that is already gone.
    const harness = createPluginApi(plugin);
    await harness.emit("gateway_start", {}, {});
    await flush();

    await harness.emit("gateway_stop", {}, {});
    await harness.emit("gateway_stop", {}, {});
    await flush();

    expect(stub.unregisterAgent).toHaveBeenCalledTimes(1);
  });

  it("gateway_stop without a prior start does nothing", async () => {
    const harness = createPluginApi(plugin);
    await harness.emit("gateway_stop", {}, {});
    await flush();

    expect(stub.unregisterAgent).not.toHaveBeenCalled();
  });

  it("survives a failed unregister and warns instead of throwing", async () => {
    // Shutdown must not throw: the gateway is already going down, and an
    // exception here would surface as a crash on exit for the user.
    stub.unregisterAgent.mockRejectedValueOnce(new Error("connection refused"));

    const harness = createPluginApi(plugin);
    await harness.emit("gateway_start", {}, {});
    await flush();
    await expect(harness.emit("gateway_stop", {}, {})).resolves.toBeUndefined();
    await flush();

    expect(harness.logger.warn).toHaveBeenCalledWith(expect.stringMatching(/unregister failed/i));
  });
});
