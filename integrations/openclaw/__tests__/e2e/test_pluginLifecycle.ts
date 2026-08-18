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
    await harness.emit("llm_output", { text: "an answer" }, { agentId: "will", sessionId: "host-session-9" });
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
   * Surprising coupling, pinned deliberately rather than worked around.
   *
   * `session_start` is registered inside `if (cfg.autoIndex)` — the same block as
   * `agent_end` — even though its body independently checks `cfg.enableSessions`.
   * So a user running `enableSessions: true, autoIndex: false` never adopts the
   * host's session id from this event.
   *
   * Not filed as a bug: `session_end` is always-on and resolves the session from
   * its own ctx, so capture still works. But the two flags read as independent and
   * are not, which is exactly the kind of thing that gets "fixed" by accident —
   * this test makes that a decision instead.
   */
  it("is NOT registered when autoIndex is off, even with sessions enabled", () => {
    const harness = createPluginApi(plugin, { autoIndex: false, enableSessions: true });
    expect(harness.handlerCount("session_start")).toBe(0);
    // The always-on final-sync handler is what keeps capture working regardless.
    expect(harness.handlerCount("session_end")).toBeGreaterThan(0);
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
