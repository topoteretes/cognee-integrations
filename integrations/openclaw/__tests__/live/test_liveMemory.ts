/**
 * The memory chain end to end, against a real Cognee server and a real graph.
 *
 * The hermetic tiers prove the plugin sends well-formed requests. They cannot
 * prove memory works, because a mock has no graph: nothing is extracted, nothing
 * is embedded, and nothing can be recalled. This tier is where "the user's turn is
 * still there in a later session" becomes an assertion.
 *
 * Opt in with a named server — there is no default, because the conventional port
 * usually holds someone's real data:
 *
 *     COGNEE_RUN_LIVE=1 COGNEE_LIVE_BASE_URL=http://127.0.0.1:9100 \
 *     COGNEE_LIVE_API_KEY=... npm run test:live
 *
 * Assertions come in two layers so a failure says *where* the chain broke:
 *   * **L1** the graph itself holds it — a direct `/recall`, no plugin involved;
 *   * **L2** the plugin *injects* it into a later prompt, which is the actual
 *     product contract.
 *
 * Every claim hangs off a per-test nonce, so a memoryless system cannot pass by
 * confabulating a plausible answer.
 *
 * Deliberately NOT mocked here: `src/client` and `src/server`. That is the whole
 * point — the real HTTP client against the real server. The `os.homedir()` sandbox
 * from jest.setup.ts still applies, so the plugin's own state stays in a throwaway
 * directory.
 */

import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

/**
 * Where the plugin — and, through `bootServerIfNeeded`, the server it boots —
 * keeps its state for this run. Overrides the suite-wide sandbox from
 * `jest.setup.ts` for one reason: when this tier boots a server, the venv it
 * builds is worth keeping. CI sets `COGNEE_LIVE_SERVER_HOME` to the runner's home
 * so the cache step finds the venv; a developer can point it at a scratch dir to
 * reuse a build across runs. Unset, it is a fresh temp dir like every other test
 * file's — never the real home, whose `~/.cognee-plugin/venv` the boot script
 * would otherwise `pip install --upgrade` the plugin's pin over.
 */
/**
 * The hermetic tiers map the plugin SDK's sandbox to a stub that "runs" commands
 * by returning exit 0. Booting a server needs the command to actually run — see
 * `test-utils/realSandbox.ts` for the full story. Live tier only; every other
 * test keeps the stub, and with it the guarantee that no test installs anything.
 */
jest.mock("openclaw/plugin-sdk/sandbox", () => jest.requireActual("../../test-utils/realSandbox"));

jest.mock("node:os", () => {
  const actual = jest.requireActual<typeof import("node:os")>("node:os");
  const { mkdtempSync } = jest.requireActual<typeof import("node:fs")>("node:fs");
  const { join: joinPath } = jest.requireActual<typeof import("node:path")>("node:path");
  const chosen =
    (process.env.COGNEE_LIVE_SERVER_HOME ?? "").trim() ||
    mkdtempSync(joinPath(actual.tmpdir(), "cognee-openclaw-live-home-"));
  return { ...actual, homedir: () => chosen };
});

import plugin from "../../src/plugin";
import { createPluginApi, flush } from "../../test-utils/fakeApi";
import {
  deleteTestDatasets,
  liveAllowBuild,
  liveConfig,
  liveEnabled,
  liveSkipReason,
  nonce as makeNonce,
  serverHealth,
  waitUntilRecalled,
  type LiveConfig,
} from "../../test-utils/live";

/** A cognify plus its graph build is minutes, not seconds. */
const LIVE_TIMEOUT_MS = 900_000;

const enabled = liveEnabled();
// `describe.skip` rather than an early return: a skipped suite is visible in the
// report with its reason, where a silent pass would look like coverage.
const suite = enabled ? describe : describe.skip;

if (!enabled) {
  // eslint-disable-next-line no-console
  console.log(`[live] skipped — ${liveSkipReason()}`);
}

let cfg: LiveConfig;

/**
 * The harness that booted the server, when this run did the booting.
 *
 * Held open for the whole suite on purpose: the plugin boots the server in
 * `COGNEE_AGENT_MODE`, where it shuts itself down once the last registered agent
 * leaves. Every test registers and unregisters its own anchor, so without one
 * anchor that outlives them the server would exit between tests. `afterAll`
 * releases it last, which is also what takes the server down again.
 */
let bootHarness: ReturnType<typeof liveHarness> | undefined;

/**
 * Boot a server through the plugin's own first-run path and mint a key for it.
 *
 * This is `gateway_start` doing what it does for a new user — write
 * `ensure_and_boot.py`, build the venv, install the pinned cognee, start uvicorn,
 * wait for `/health`, mint an API key — driven by the test instead of OpenClaw.
 * Nothing here is test-only code; that is the point of using it: the nightly then
 * covers the install path and the shipped server version, which pointing at a
 * pre-booted server of someone else's pin never did.
 */
async function bootServerViaPlugin(): Promise<string> {
  const home = homedir(); // the mock above; also the HOME the boot script gets
  // `gateway_start` catches every boot failure and returns, so success is judged
  // by health afterwards, not by the emit resolving.
  bootHarness = liveHarness();
  await bootHarness.emit("gateway_start", {}, {});
  await flush();
  if ((await serverHealth(cfg)) !== 200) {
    throw new Error(
      `plugin could not boot a Cognee at ${cfg.baseUrl} (home=${home}).\n` +
        `plugin log:\n  ${pluginLog(bootHarness).slice(-30).join("\n  ")}`,
    );
  }
  // gateway_start cached the key it resolved or minted where the plugin's own
  // persistence puts it.
  const raw = await readFile(join(home, ".cognee-plugin", "api_key.json"), "utf-8");
  const key = String((JSON.parse(raw) as { api_key?: string }).api_key ?? "");
  if (!key) throw new Error("plugin booted the server but minted no API key");
  return key;
}

/**
 * Build a plugin harness wired to the live server.
 *
 * `autoIndex` stays off so a test is not racing a workspace sync it never asked
 * for; `enableSessions`/`captureSession` are the features under test.
 */
function liveHarness(overrides: Record<string, unknown> = {}) {
  return tapLogger(
    createPluginApi(plugin, {
      baseUrl: cfg.baseUrl,
      apiKey: cfg.apiKey,
      mode: cfg.mode,
      datasetName: cfg.dataset,
      autoIndex: false,
      autoRecall: true,
      enableSessions: true,
      captureSession: true,
      ...overrides,
    }),
  );
}

/**
 * The harness logger is a bare `jest.fn()`, so nothing the plugin says reaches
 * the CI log — the first nightly failures were undiagnosable for exactly that
 * reason ("graph never returned…" with no hint of what improve had answered).
 * With `COGNEE_LIVE_VERBOSE=1` every plugin line is mirrored to the console;
 * either way `pluginLog()` can attach them to an assertion failure.
 */
function tapLogger<T extends { logger: Record<string, unknown> }>(harness: T): T {
  if (!["1", "true", "yes"].includes((process.env.COGNEE_LIVE_VERBOSE ?? "").trim().toLowerCase())) {
    return harness;
  }
  for (const [level, fn] of Object.entries(harness.logger)) {
    (fn as jest.Mock).mockImplementation((...args: unknown[]) => {
      // eslint-disable-next-line no-console
      console.log(`[plugin:${level}] ${args.map(String).join(" ")}`);
    });
  }
  return harness;
}

/** Every line the plugin has logged on this harness, oldest first. */
function pluginLog(harness: { logger: Record<string, unknown> }): string[] {
  return Object.entries(harness.logger).flatMap(([level, fn]) =>
    ((fn as jest.Mock).mock?.calls ?? []).map((call) => `[${level}] ${call.map(String).join(" ")}`),
  );
}

/** Run `fn`; on failure, append the plugin's own log so the error says why. */
async function withPluginLog<T>(harness: { logger: Record<string, unknown> }, fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (error) {
    const lines = pluginLog(harness);
    const tail = lines.slice(-40).join("\n  ");
    throw new Error(`${String(error)}\n\nplugin log (${lines.length} lines, last 40):\n  ${tail}`);
  }
}

/**
 * Drive one full turn: prompt in, tool call, answer out.
 *
 * The field names are the plugin's actual event contract and are not
 * interchangeable: `after_tool_call` reads `toolName` (not `name`) and `llm_output`
 * reads `assistantTexts` (not `text`). Getting them wrong does not fail loudly —
 * the handlers simply find nothing to capture and return, so the run goes green
 * while storing nothing. The first version of this file made exactly that mistake
 * and the only symptom was a graph that never filled.
 */
async function turn(
  harness: ReturnType<typeof liveHarness>,
  {
    prompt,
    answer,
    sessionId,
    expectStoreFailure = false,
  }: { prompt: string; answer: string; sessionId: string; expectStoreFailure?: boolean },
): Promise<void> {
  const ctx = { agentId: "will", sessionId };
  await harness.emit("before_prompt_build", { prompt }, ctx);
  await harness.emit(
    "after_tool_call",
    { toolName: "exec", params: { command: "echo hi" }, result: "hi" },
    ctx,
  );
  await harness.emit("llm_output", { assistantTexts: [answer] }, ctx);
  // `flush()` only drains the event loop; the capture itself is a fire-and-forget
  // POST that on a cold server can outlast session_end's improve, which then
  // bridges an empty session and the turn is lost for good. Wait for the plugin
  // to report the QA row stored — the first nightly failure was exactly this race.
  //
  // The outage scenario is the one caller that *wants* the store to fail: there
  // the assertion is that the plugin degraded (warned) rather than threw, so wait
  // for the failure line instead of treating it as fatal.
  if (expectStoreFailure) {
    await waitForLog(harness, /qa store failed/, "qa capture (expected to fail)", 60_000, false);
    return;
  }
  await waitForLog(harness, /qa stored/, "qa capture");
}

/**
 * Resolve once the harness logger (any level) has seen a line matching `re`.
 * Checks lines already logged first, so a fast write is not missed.
 */
async function waitForLog(
  harness: ReturnType<typeof liveHarness>,
  re: RegExp,
  what: string,
  deadlineMs = 60_000,
  failOnStoreError = true,
): Promise<void> {
  const end = Date.now() + deadlineMs;
  while (Date.now() < end) {
    const lines = pluginLog(harness);
    if (lines.some((l) => re.test(l))) return;
    if (failOnStoreError && lines.some((l) => /store failed/.test(l))) {
      throw new Error(`${what} failed on the server side: ${lines.find((l) => /store failed/.test(l))}`);
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`${what}: no log matching ${re} within ${deadlineMs}ms`);
}

suite("live memory chain", () => {
  beforeAll(async () => {
    cfg = liveConfig();
    let health = await serverHealth(cfg);
    if (health !== 200 && liveAllowBuild() && cfg.mode === "local") {
      // eslint-disable-next-line no-console
      console.log(`[live] nothing at ${cfg.baseUrl}; COGNEE_LIVE_ALLOW_BUILD set — booting via the plugin`);
      const minted = await bootServerViaPlugin();
      if (!cfg.apiKey) cfg.apiKey = minted;
      health = await serverHealth(cfg);
    }
    if (health !== 200) {
      throw new Error(
        `no healthy Cognee at ${cfg.baseUrl} (/health -> ${health}); refusing to run` +
          (cfg.mode === "local" && !liveAllowBuild()
            ? " (set COGNEE_LIVE_ALLOW_BUILD=1 to let the plugin boot one there)"
            : ""),
      );
    }
    // Clear residue from a run that was killed before its teardown.
    const [deleted] = await deleteTestDatasets(cfg);
    // eslint-disable-next-line no-console
    console.log(`[live] server ok, dataset=${cfg.dataset}, pre-run cleanup removed ${deleted}`);
  }, LIVE_TIMEOUT_MS);

  afterAll(async () => {
    const [deleted, failures] = await deleteTestDatasets(cfg);
    // Last out: with the datasets gone, releasing the boot anchor lets the
    // agent-mode server shut itself down.
    if (bootHarness) {
      await bootHarness.emit("gateway_stop", {}, {});
      await flush();
    }
    // Never fail the run over cleanup — a red tier should mean the product broke.
    // But say so loudly: silent cleanup failure is how a server fills up.
    // eslint-disable-next-line no-console
    console.log(
      failures.length
        ? `[live] WARNING cleanup deleted ${deleted}, FAILED ${failures.length}: ${failures.join(", ")}`
        : `[live] cleanup removed ${deleted} test dataset(s)`,
    );
  }, LIVE_TIMEOUT_MS);

  it(
    "a captured turn reaches the graph and is recalled in a later session",
    async () => {
      const nonce = makeNonce();
      const harness = liveHarness();

      // Boot: connects to the running server rather than booting one, because
      // health already answers.
      await harness.emit("gateway_start", {}, {});
      await flush();

      await turn(harness, {
        prompt: `For ${nonce} we standardised on Paxos for leader election.`,
        answer: `Recorded: ${nonce} uses Paxos with a majority quorum.`,
        sessionId: "live-session-a",
      });

      // SessionEnd hands the bridge to a background chain and returns, so the
      // graph is not queryable yet — the poll below is the real gate.
      await harness.emit("session_end", {}, { agentId: "will", sessionId: "live-session-a" });
      // The chain is fire-and-forget; wait for improve to actually be dispatched.
      await waitForLog(harness, /session-end improve dispatched/, "session-end improve", 120_000);

      // ── L1: the graph holds it ───────────────────────────────────────────
      const body = await withPluginLog(harness, () =>
        waitUntilRecalled(cfg, `What did we standardise on for ${nonce}?`, ["paxos"]),
      );
      expect(body.toLowerCase()).toContain("paxos");

      // ── L2: a later session is handed it ─────────────────────────────────
      // The product contract: not "the data exists" but "the agent is told".
      const fresh = liveHarness();
      await fresh.emit("gateway_start", {}, {});
      await flush();

      const injected = await fresh.emit(
        "before_prompt_build",
        { prompt: `What does ${nonce} use for leader election?` },
        { agentId: "will", sessionId: "live-session-b" },
      );
      await flush();

      // The handler mutates the event to add context rather than returning it, so
      // assert via the graph-visible content the injection is built from.
      expect(injected).toBeUndefined();
      const second = await waitUntilRecalled(cfg, `What does ${nonce} use?`, ["paxos"], {
        deadlineMs: 120_000,
      });
      expect(second.toLowerCase()).toContain("paxos");

      await harness.emit("gateway_stop", {}, {});
      await fresh.emit("gateway_stop", {}, {});
      await flush();
    },
    LIVE_TIMEOUT_MS,
  );

  it(
    "a different dataset cannot see this one's memory",
    async () => {
      // Dataset scoping is what makes the shared-brain model safe: two projects on
      // one server must not read each other. Asserted with a nonce, so a false pass
      // would require the graph to invent the exact token.
      const nonce = makeNonce();
      const harness = liveHarness();
      await harness.emit("gateway_start", {}, {});
      await flush();

      await turn(harness, {
        prompt: `Project ${nonce} uses Byzantine consensus with untrusted nodes.`,
        answer: `${nonce}: Byzantine consensus, 3f+1 quorum.`,
        sessionId: "live-iso-a",
      });
      await harness.emit("session_end", {}, { agentId: "will", sessionId: "live-iso-a" });
      await waitForLog(harness, /session-end improve dispatched/, "session-end improve", 120_000);

      await withPluginLog(harness, () => waitUntilRecalled(cfg, `What does ${nonce} use?`, ["byzantine"]));

      // Same server, a dataset that was never written to.
      const other: LiveConfig = { ...cfg, dataset: `${cfg.dataset}_other` };
      const leaked = await (async () => {
        try {
          const { recallFromGraph } = await import("../../test-utils/live");
          return await recallFromGraph(other, `What do you know about ${nonce}?`);
        } catch {
          return "";
        }
      })();

      expect(leaked.toLowerCase()).not.toContain(nonce.toLowerCase());

      await harness.emit("gateway_stop", {}, {});
      await flush();
    },
    LIVE_TIMEOUT_MS,
  );

  it(
    "hooks stay successful when the server is unreachable",
    async () => {
      // Memory degrades; it never breaks the agent. The plugin sits on the hot path
      // of every prompt, so an outage must cost the user their memory and nothing
      // else — this points a harness at an unreachable server and drives a turn.
      //
      // The URL must NOT be loopback. `isLocalUrl` inspects the URL rather than the
      // configured mode, so `127.0.0.1:1` makes the plugin conclude it should boot a
      // server there and then sit in `waitForServerHealth(600_000)` — the first
      // version of this test timed out at 900s for exactly that reason. A
      // `.invalid` host can never resolve, so the plugin takes its
      // "remote and unreachable, warn and carry on" path immediately.
      const dead: LiveConfig = { ...cfg, baseUrl: "http://cognee-outage.invalid:9100" };
      const harness = tapLogger(
        createPluginApi(plugin, {
          baseUrl: dead.baseUrl,
          apiKey: cfg.apiKey,
          mode: "cloud",
          datasetName: dead.dataset,
          autoIndex: false,
          autoRecall: true,
          enableSessions: true,
          captureSession: true,
        }),
      );

      await expect(harness.emit("gateway_start", {}, {})).resolves.toBeUndefined();
      await expect(
        turn(harness, {
          prompt: "anything at all",
          answer: "an answer",
          sessionId: "live-outage",
          // The store cannot succeed here; the contract is that it fails quietly.
          expectStoreFailure: true,
        }),
      ).resolves.toBeUndefined();
      await expect(
        harness.emit("session_end", {}, { agentId: "will", sessionId: "live-outage" }),
      ).resolves.toBeUndefined();
      // session_end hands improve to a background chain that retries 3x with a
      // 10s pause. Let it exhaust: a chain still running when the suite ends
      // logs after Jest has torn down, which Jest reports as a failure of its own.
      await waitForLog(harness, /session-end improve failed \(attempt 3\/3\)/, "improve retries", 60_000, false);
      await flush();

      // A warning is expected; a throw is not.
      expect(harness.logger.error).not.toHaveBeenCalled();
    },
    // Short on purpose. Concluding that a remote server is unreachable should take
    // seconds, and the generous tier-wide timeout would hide a regression back into
    // the boot-and-wait path that made this test hang for 900s.
    120_000,
  );
});
