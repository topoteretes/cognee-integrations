/**
 * A stand-in for `OpenClawPluginApi`, so tests can drive the plugin's real
 * lifecycle without OpenClaw.
 *
 * The plugin's whole surface is `register(api)` plus `api.on(event, handler)` for
 * nine events, so "run the plugin" reduces to: register against a fake api,
 * collect the handlers it subscribed, then call them. That makes the OpenClaw
 * equivalent of an end-to-end test cheap — in-process, milliseconds, no
 * subprocess — where the Python integrations have to spawn a hook per event.
 *
 * Extracted from `__tests__/test_sessionCapture.ts`, which had grown a working
 * version of this privately; every new lifecycle test needed it, so it belongs
 * here rather than being copied.
 *
 * NOTE: `jest.mock()` is hoisted per module and cannot be called from a helper.
 * Tests still declare their own `jest.mock("../src/client")` /
 * `jest.mock("../src/server", ...)` — see `serverMock()` below for the standard
 * server stub, which is the one every lifecycle test needs.
 */

export type HookHandler = (event: unknown, ctx: unknown) => Promise<unknown> | unknown;

export interface FakePluginApi {
  /** The object handed to `plugin.register()`. */
  api: Record<string, unknown>;
  /** Fire one event at every handler registered for it, in subscription order. */
  emit: (name: string, event?: unknown, ctx?: unknown) => Promise<void>;
  /** Event names the plugin actually subscribed to. */
  subscribed: () => string[];
  /** Handler count for one event — 0 proves the plugin opted out under this config. */
  handlerCount: (name: string) => number;
  logger: { info: jest.Mock; warn: jest.Mock; debug: jest.Mock; error: jest.Mock };
  registerCli: jest.Mock;
  registerMemoryFlushPlan: jest.Mock;
  registerService: jest.Mock;
}

/**
 * Plugin config used unless a test overrides it.
 *
 * Auto-index and auto-recall are off so a test opts into the background work it
 * wants to observe. Leaving them on makes every test race a startup sync it did
 * not ask for, which is the usual cause of a lifecycle test that passes alone and
 * fails in a suite.
 */
export const DEFAULT_PLUGIN_CONFIG: Record<string, unknown> = {
  autoIndex: false,
  autoRecall: false,
  enableSessions: true,
  captureSession: true,
  datasetName: "testds",
};

/**
 * Register the plugin against a fake api and return the driver.
 *
 * `pluginConfig` is merged over `DEFAULT_PLUGIN_CONFIG`, so a test names only the
 * flag it is exercising.
 */
export function createPluginApi(
  plugin: { register: (api: never) => unknown },
  pluginConfig: Record<string, unknown> = {},
): FakePluginApi {
  const handlers = new Map<string, HookHandler[]>();

  const logger = {
    info: jest.fn(),
    warn: jest.fn(),
    debug: jest.fn(),
    error: jest.fn(),
  };
  const registerCli = jest.fn();
  const registerMemoryFlushPlan = jest.fn();
  const registerService = jest.fn();

  const api: Record<string, unknown> = {
    id: "cognee-openclaw",
    name: "Memory (Cognee)",
    source: "test",
    config: {},
    pluginConfig: { ...DEFAULT_PLUGIN_CONFIG, ...pluginConfig },
    runtime: {},
    logger,
    registerMemoryFlushPlan,
    registerCli,
    registerService,
    on: jest.fn((name: string, fn: HookHandler) => {
      const list = handlers.get(name) ?? [];
      list.push(fn);
      handlers.set(name, list);
    }),
  };

  plugin.register(api as never);

  return {
    api,
    emit: async (name, event = {}, ctx = {}) => {
      for (const fn of handlers.get(name) ?? []) await fn(event, ctx);
    },
    subscribed: () => [...handlers.keys()],
    handlerCount: (name) => (handlers.get(name) ?? []).length,
    logger,
    registerCli,
    registerMemoryFlushPlan,
    registerService,
  };
}

/**
 * The standard `src/server` stub for lifecycle tests.
 *
 * Spread into a `jest.mock("../src/server", () => ({ ...serverMock() }))` factory.
 * Stubbing it is not optional: the real module writes `ensure_and_boot.py` under
 * `~/.cognee-plugin`, creates a venv, installs cognee and boots uvicorn — against
 * the developer's real home, because it resolves `homedir()` at module load.
 */
export function serverMock(): Record<string, jest.Mock> {
  return {
    bootServerIfNeeded: jest.fn(async () => {}),
    waitForServerHealth: jest.fn(async () => {}),
    isLocalUrl: jest.fn(() => true),
    resolveOrMintApiKey: jest.fn(async () => "test-api-key"),
    spawnExitWatcher: jest.fn(async () => {}),
    exitWatcherPidfilePath: jest.fn((name: string) => `/tmp/exit-watchers/${name}.pid`),
  };
}

/**
 * Let fire-and-forget promise chains settle.
 *
 * Several handlers deliberately do not await their background work (so the prompt
 * path is never blocked by memory), which means assertions need to yield to the
 * microtask queue a few times before the effect is observable.
 */
export async function flush(rounds = 10): Promise<void> {
  for (let i = 0; i < rounds; i++) await new Promise((r) => setTimeout(r, 0));
}
