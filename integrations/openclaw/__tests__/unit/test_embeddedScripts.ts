/**
 * The two Python scripts the plugin writes and runs: ensure_and_boot.py (install
 * cognee, start the local server) and exit-watcher.py (bridge + unregister when
 * the gateway dies). Both are inlined as strings, so these tests read them
 * as-is and run the parts that can run on a dev machine or CI runner.
 */

import { execFileSync, spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { MockCognee } from "../../test-utils/mockCognee";

jest.mock("openclaw/plugin-sdk/sandbox", () => ({
  runPluginCommandWithTimeout: jest.fn(async () => ({ code: 0, stdout: "", stderr: "" })),
  resolvePreferredOpenClawTmpDir: () => "/tmp/openclaw",
}));

const PYTHON = ["python3", "python"].find((p) => spawnSync(p, ["--version"]).status === 0);
const itPy = PYTHON ? it : it.skip;

describe("ensure_and_boot.py", () => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const { ENSURE_SCRIPT_CONTENT: script } = require("../../src/server") as { ENSURE_SCRIPT_CONTENT: string };

  it("pins cognee 1.6.5", () => {
    expect(script).toContain("COGNEE_VERSION = '1.6.5'");
  });

  it("never lets uv download an interpreter, and asks for cognee's Python range instead of one minor", () => {
    expect(script).toContain("uv_env['UV_PYTHON_DOWNLOADS'] = 'never'");
    expect(script).toContain("UV_PYTHON_REQUEST = '>=%d.%d,<%d.%d'");
    expect(script).not.toContain("'--python', '3.12'");
    expect(script).not.toMatch(/curl|astral\.sh|install\.sh/);
  });

  it("starts uvicorn from the plugin state dir, so a project .env cannot override its env (cognee >= 1.6.2)", () => {
    expect(script).toMatch(/\[VENV_PYTHON, '-m', 'uvicorn', 'cognee\.api\.client:app', '--port', PORT\],\n\s+env=env, cwd=BASE,/);
  });

  it("loads ~/.cognee/.env into the server's env before uvicorn starts", () => {
    const loadAt = script.indexOf("    load_cognee_env(env)");
    expect(loadAt).toBeGreaterThan(script.indexOf("    env = dict(os.environ)"));
    expect(loadAt).toBeLessThan(script.indexOf("'cognee.api.client:app'"));
    // Before the literal default user, so the file's DEFAULT_USER_* win like an export.
    expect(loadAt).toBeLessThan(script.indexOf("env.setdefault('DEFAULT_USER_PASSWORD'"));
  });

  itPy("reads ~/.cognee/.env like the claude-code/codex plugins: exports win, risky names skipped", () => {
    const dir = mkdtempSync(join(tmpdir(), "ensure-env-"));
    try {
      const scriptPath = join(dir, "ensure.py");
      writeFileSync(scriptPath, script);
      const envFile = join(dir, "cognee.env");
      writeFileSync(envFile, [
        "# a comment",
        'export LLM_API_KEY="sk-from-file"',
        "LLM_MODEL='openai/gpt-4o-mini'",
        "EMBEDDING_PROVIDER = fastembed",
        "ALREADY_SET=from-file",
        "PATH=/evil",
        "LD_PRELOAD=/evil.so",
        "bad-key=1",
        "no equals sign",
      ].join("\n"));
      const probe = [
        "import json, os, sys",
        "src = open(sys.argv[1]).read()",
        "body = src[src.index('ENV_DENY = {'):src.index('def boot_server():')]",
        "ns = {}",
        "exec('import os\\n' + body, ns)",
        "os.environ['COGNEE_ENV_FILE'] = sys.argv[2]",
        "env = {'ALREADY_SET': 'from-shell', 'PATH': '/usr/bin'}",
        "ns['load_cognee_env'](env)",
        "print(json.dumps(env, sort_keys=True))",
      ].join("\n");
      const env = JSON.parse(execFileSync(PYTHON!, ["-I", "-c", probe, scriptPath, envFile], { encoding: "utf-8" }));
      expect(env).toEqual({
        ALREADY_SET: "from-shell",
        EMBEDDING_PROVIDER: "fastembed",
        LLM_API_KEY: "sk-from-file",
        LLM_MODEL: "openai/gpt-4o-mini",
        PATH: "/usr/bin",
      });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  itPy("falls back to pip in a venv uv built, seeding pip first, and keeps uv's own error", () => {
    const home = mkdtempSync(join(tmpdir(), "ensure-fallback-"));
    try {
      const base = join(home, ".cognee-plugin");
      const scriptPath = join(home, "ensure.py");
      // Everything but the module body that installs and boots on import.
      writeFileSync(scriptPath, script.replace(/\nif needs_install\(\):\n    install_cognee\(\)\nboot_server\(\)\s*$/, "\n"));
      // A fake uv: `uv venv` makes a venv WITHOUT pip (as uv does); `uv pip install` fails.
      const fakeUv = join(base, "uv", "uv");
      execFileSync("mkdir", ["-p", join(base, "uv")]);
      writeFileSync(fakeUv, [
        "#!/bin/sh",
        'if [ "$1" = "venv" ]; then exec ' + PYTHON + ' -m venv --without-pip "$2"; fi',
        'echo "error: Failed to fetch: https://pypi.org/simple/cognee/ (offline test)" >&2',
        "exit 2",
      ].join("\n"), { mode: 0o755 });
      const probe = [
        "import json, os, sys",
        "sys.argv = ['ensure.py', '9999', '--daemon']",
        "ns = {'__file__': sys.argv[0], '__name__': 'ensure'}",
        "exec(open(sys.argv[0] if False else os.environ['ENSURE']).read(), ns)",
        "ok = ns['install_cognee']()",
        "venv_py = ns['VENV_PYTHON']",
        "has_pip = __import__('subprocess').run([venv_py, '-m', 'pip', '--version'], capture_output=True).returncode == 0",
        "err = json.load(open(ns['ERROR_MARKER']))['error'] if os.path.exists(ns['ERROR_MARKER']) else ''",
        "print(json.dumps({'ok': ok, 'has_pip': has_pip, 'error': err}))",
      ].join("\n");
      const out = JSON.parse(execFileSync(PYTHON!, ["-c", probe], {
        encoding: "utf-8",
        // No index: the pip fallback fails fast and offline, after seeding pip.
        env: { ...process.env, HOME: home, ENSURE: scriptPath, PIP_NO_INDEX: "1", PIP_DISABLE_PIP_VERSION_CHECK: "1" },
        timeout: 240_000,
      }).trim().split("\n").pop()!);
      expect(out.ok).toBe(false);
      expect(out.has_pip).toBe(true); // ensurepip ran on the uv-built venv
      expect(out.error).not.toMatch(/No module named pip/);
      expect(out.error).toMatch(/^uv failed first: .*Failed to fetch/);
      expect(out.error).toMatch(/installing cognee with pip failed \(exit 1\): .*cognee==1\.6\.5/);
    } finally {
      rmSync(home, { recursive: true, force: true });
    }
  }, 300_000);

  itPy("is valid Python, and its interpreter probe only accepts 3.10-3.14", () => {
    const dir = mkdtempSync(join(tmpdir(), "ensure-script-"));
    try {
      const path = join(dir, "ensure.py");
      writeFileSync(path, script);
      // Run only the helpers: the module body would daemonize and install.
      const probe = [
        "import ast, sys",
        "src = open(sys.argv[1]).read()",
        "ast.parse(src)",
        "body = src[src.index('PY_MIN = (3, 10)'):src.index('def install_cognee():')]",
        "ns = {}",
        "exec('import subprocess, sys, os\\n' + body, ns)",
        "print(ns['UV_PYTHON_REQUEST'])",
        "exe = ns['find_host_python']()",
        "v = ns['python_version'](exe) if exe else None",
        "print('none' if not exe else ('ok' if (3, 10) <= v <= (3, 14) else 'bad'))",
      ].join("\n");
      const out = execFileSync(PYTHON!, ["-I", "-c", probe, path], { encoding: "utf-8" }).trim().split("\n");
      expect(out[0]).toBe(">=3.10,<3.15");
      expect(["ok", "none"]).toContain(out[1]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("exit-watcher.py", () => {
  let home: string;
  let mock: MockCognee;

  beforeEach(async () => {
    home = mkdtempSync(join(tmpdir(), "exit-watcher-"));
    mock = await MockCognee.start();
  });
  afterEach(async () => {
    await mock.close();
    rmSync(home, { recursive: true, force: true });
  });

  /** Write the watcher via spawnExitWatcher, then run it in the foreground against `mock`. */
  async function runWatcher(bridge?: { datasetName: string; cogneeSessionId: string }): Promise<void> {
    jest.resetModules();
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const server = require("../../src/server");
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { runPluginCommandWithTimeout } = require("openclaw/plugin-sdk/sandbox");
    const name = "s1-will";
    const pidfile = server.exitWatcherPidfilePath(name) as string;
    await server.spawnExitWatcher({
      gatewayPid: 2 ** 22 + 12345, // not a live pid: the watcher acts at once
      agentSessionName: name,
      baseUrl: mock.url,
      pidfilePath: pidfile,
      datasetName: "testds",
      cogneeSessionId: "open_claw_s1",
      logger: {},
    });
    if (bridge) await server.writeExitWatcherBridge(name, bridge);
    const { argv } = runPluginCommandWithTimeout.mock.calls.at(-1)[0];
    // Async spawn: the mock server lives in this process and must keep serving.
    const status = await new Promise<number | null>((resolve) => {
      const child = spawn(PYTHON!, [argv[1], argv[2], "--daemon"], { env: { ...process.env, HOME: home }, stdio: "ignore" });
      const timer = setTimeout(() => child.kill(), 20_000);
      child.on("exit", (code) => { clearTimeout(timer); resolve(code); });
    });
    expect(status).toBe(0);
    expect(existsSync(`${pidfile}.bridge.json`)).toBe(false); // cleaned up with the pidfile
  }

  itPy("bridges the session it was spawned with", async () => {
    await runWatcher();
    expect(mock.assertCalled("POST", "/improve").json).toMatchObject({ dataset_name: "testds", session_ids: ["open_claw_s1"] });
    mock.assertCalled("POST", "/agents/unregister");
  });

  itPy("bridges the session a later dataset switch moved the conversation to", async () => {
    await runWatcher({ datasetName: "proj-a", cogneeSessionId: "open_claw_s1__2" });
    expect(mock.assertCalled("POST", "/improve").json).toMatchObject({ dataset_name: "proj-a", session_ids: ["open_claw_s1__2"] });
  });

  it("writeExitWatcherBridge records the current target next to the pidfile", async () => {
    jest.resetModules();
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const server = require("../../src/server");
    await server.writeExitWatcherBridge("s1-will", { datasetName: "proj-a", cogneeSessionId: "open_claw_s1__2" });
    const written = JSON.parse(readFileSync(`${server.exitWatcherPidfilePath("s1-will")}.bridge.json`, "utf-8"));
    expect(written).toEqual({ dataset_name: "proj-a", cognee_session_id: "open_claw_s1__2" });
  });
});
