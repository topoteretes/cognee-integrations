import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

jest.mock("openclaw/plugin-sdk/sandbox", () => ({
  runPluginCommandWithTimeout: jest.fn(async () => ({ code: 0, stdout: "", stderr: "" })),
  resolvePreferredOpenClawTmpDir: () => "/tmp/openclaw",
}));

const originalHome = process.env.HOME;
let home: string;
beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "cognee-watcher-"));
  process.env.HOME = home;
  jest.resetModules();
});
afterEach(() => {
  process.env.HOME = originalHome;
  rmSync(home, { recursive: true, force: true });
});

it("passes the API key via the environment, never on argv", async () => {
  const { spawnExitWatcher } = require("../../src/server");
  const { runPluginCommandWithTimeout } = require("openclaw/plugin-sdk/sandbox");
  await spawnExitWatcher({
    gatewayPid: 1234,
    agentSessionName: "s-main",
    baseUrl: "http://127.0.0.1:8000",
    apiKey: "secret-key-123",
    pidfilePath: join(home, "w.pid"),
    logger: {},
  });
  const opts = runPluginCommandWithTimeout.mock.calls[0][0];
  expect(opts.argv.join(" ")).not.toContain("secret-key-123");
  expect(JSON.parse(opts.argv[2])).not.toHaveProperty("api_key");
  expect(opts.env.COGNEE_API_KEY).toBe("secret-key-123");
  const script = readFileSync(opts.argv[1], "utf-8");
  expect(script).toContain("os.environ.get('COGNEE_API_KEY'");
});
