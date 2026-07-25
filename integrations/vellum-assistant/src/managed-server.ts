/**
 * Managed Cognee server lifecycle.
 *
 * When the plugin owns the server (`config.mode === "local"`), the init hook
 * calls `ensureLocalServer` to bring a local Cognee server up before the
 * memory hooks run. The flow is:
 *
 *   1. Short-circuit if the server is already reachable.
 *   2. Resolve a Python interpreter (from the spec, then PATH).
 *   3. Provision a venv on first run (python -m venv + pip install cognee).
 *   4. Spawn the server (uvicorn) detached, redirecting logs to a file.
 *   5. Poll the healthcheck until it comes up (or time out).
 *
 * Hard floor: this cannot conjure a Python runtime. If no interpreter is
 * found, it logs an actionable error and returns false — the memory hooks
 * then degrade to no-ops, exactly as they do for an unreachable remote server.
 *
 * Best-effort: never throws. Returns whether the server is reachable.
 */

import type { PluginLogger } from "@vellumai/plugin-api";
import { existsSync, mkdirSync, openSync } from "node:fs";
import { join } from "node:path";
import { spawn } from "bun";

import type { CogneePluginConfig, CogneeServerSpec } from "./plugin-common.ts";
import {
  hookLog,
  writeServerPid,
  serverLogPath,
  pluginStateDir,
  resolveLlmApiKey,
} from "./plugin-common.ts";
import { backendReachable } from "./cognee-client.ts";

/** The hook logger surface (pino-compatible) handed in from the init context. */
type Logger = PluginLogger;

const PROVISION_TIMEOUT_MS = 10 * 60_000; // venv create + pip install can be slow
const HEALTH_TIMEOUT_MS = 90_000;
const HEALTH_POLL_MS = 1_500;

/**
 * The interpreter inside a venv (POSIX layout; Vellum envs are macOS/Linux).
 */
function venvPython(venvDir: string): string {
  return join(venvDir, "bin", "python");
}

/**
 * Run a command to completion, capturing exit code + tail of output.
 */
async function run(
  cmd: string[],
  opts: { cwd?: string; timeoutMs?: number; env?: Record<string, string> } = {},
): Promise<{ ok: boolean; code: number; output: string }> {
  try {
    const proc = spawn({
      cmd,
      cwd: opts.cwd,
      env: opts.env ? { ...process.env, ...opts.env } : process.env,
      stdout: "pipe",
      stderr: "pipe",
    });

    let timed = false;
    const timer = opts.timeoutMs
      ? setTimeout(() => {
          timed = true;
          proc.kill();
        }, opts.timeoutMs)
      : null;

    const [stdout, stderr, code] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
      proc.exited,
    ]);
    if (timer) clearTimeout(timer);

    const output = `${stdout}\n${stderr}`.trim().slice(-800);
    return { ok: !timed && code === 0, code, output };
  } catch (err) {
    return { ok: false, code: -1, output: String(err).slice(0, 400) };
  }
}

/**
 * Resolve a usable Python interpreter. Tries the configured command, then a
 * few common fallbacks, verifying each actually runs. Returns the resolved
 * command or null if none works.
 */
async function resolvePython(preferred: string): Promise<string | null> {
  const candidates = [preferred, "python3", "python"].filter(
    (c, i, arr) => c && arr.indexOf(c) === i,
  );
  for (const cand of candidates) {
    const res = await run([cand, "--version"], { timeoutMs: 5_000 });
    if (res.ok) return cand;
  }
  return null;
}

/**
 * Provision the venv if it doesn't exist: create it, then pip install cognee.
 * Returns true when the venv is ready (interpreter present + cognee importable).
 */
async function ensureVenv(
  python: string,
  spec: CogneeServerSpec,
  logger: Logger,
): Promise<boolean> {
  const py = venvPython(spec.venvDir);

  if (existsSync(py)) {
    // Venv exists — assume cognee is installed (provisioned on a prior boot).
    // A cheap import check guards against a half-provisioned venv.
    const check = await run([py, "-c", "import cognee"], { timeoutMs: 15_000 });
    if (check.ok) return true;
    logger.warn(
      { venvDir: spec.venvDir },
      "managed cognee venv exists but cognee isn't importable — reinstalling",
    );
  } else {
    logger.info(
      { venvDir: spec.venvDir, python },
      "provisioning managed cognee venv (first run — this can take a few minutes)",
    );
    mkdirSync(spec.venvDir, { recursive: true });
    const created = await run([python, "-m", "venv", spec.venvDir], {
      timeoutMs: PROVISION_TIMEOUT_MS,
    });
    if (!created.ok) {
      logger.error(
        { venvDir: spec.venvDir, output: created.output },
        "failed to create cognee venv — managed server unavailable",
      );
      hookLog("managed_venv_create_failed", { output: created.output });
      return false;
    }
  }

  // Ensure pip is available in the venv. Some base images (e.g. Debian
  // without python3-pip) create venvs without pip. Bootstrap it via
  // get-pip.py if the venv python can't import pip.
  const pipCheck = await run([py, "-c", "import pip"], { timeoutMs: 10_000 });
  if (!pipCheck.ok) {
    logger.info({ venvDir: spec.venvDir }, "pip not found in venv — bootstrapping via get-pip.py");
    const bootstrapped = await run(
      [py, "-c", "import urllib.request, sys; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', '/tmp/get-pip.py'); import subprocess; sys.exit(subprocess.call([sys.executable, '/tmp/get-pip.py']))"],
      { timeoutMs: 120_000 },
    );
    if (!bootstrapped.ok) {
      logger.error(
        { output: bootstrapped.output },
        "pip bootstrap failed — managed server unavailable",
      );
      hookLog("managed_pip_bootstrap_failed", { output: bootstrapped.output });
      return false;
    }
  }

  // Install (or repair) cognee + uvicorn into the venv. Cognee 1.3+ may
  // not pull uvicorn as a dependency, so install it explicitly.
  logger.info({ venvDir: spec.venvDir }, "installing cognee + uvicorn into managed venv");
  const pip = await run(
    [py, "-m", "pip", "install", "--upgrade", "cognee", "uvicorn"],
    { timeoutMs: PROVISION_TIMEOUT_MS },
  );
  if (!pip.ok) {
    logger.error(
      { output: pip.output },
      "pip install cognee+uvicorn failed — managed server unavailable",
    );
    hookLog("managed_pip_install_failed", { output: pip.output });
    return false;
  }

  hookLog("managed_venv_provisioned", { venvDir: spec.venvDir });
  return true;
}

/**
 * Spawn the Cognee server (uvicorn) from the venv, detached, with stdout/stderr
 * redirected to the server log. The child is parented to the daemon process;
 * the shutdown hook kills it by the PID recorded here.
 */
function spawnServer(spec: CogneeServerSpec, logger: Logger): number | null {
  try {
    mkdirSync(pluginStateDir(), { recursive: true });
    const logFd = openSync(serverLogPath(), "a");
    const py = venvPython(spec.venvDir);

    const proc = spawn({
      cmd: [
        py,
        "-m",
        "uvicorn",
        "cognee.api.client:app",
        "--host",
        spec.host,
        "--port",
        String(spec.port),
      ],
      env: { ...process.env, ...spec.env },
      stdout: logFd,
      stderr: logFd,
      stdin: "ignore",
    });

    // Let the child outlive this hook's await graph.
    proc.unref();

    const pid = proc.pid;
    if (pid) writeServerPid(pid);
    logger.info(
      { pid, host: spec.host, port: spec.port, log: serverLogPath() },
      "spawned managed cognee server",
    );
    hookLog("managed_server_spawned", { pid, host: spec.host, port: spec.port });
    return pid ?? null;
  } catch (err) {
    logger.error({ error: String(err).slice(0, 200) }, "failed to spawn managed cognee server");
    hookLog("managed_server_spawn_failed", { error: String(err).slice(0, 200) });
    return null;
  }
}

/**
 * Poll the healthcheck until the server is reachable or the deadline passes.
 */
async function waitForServer(baseUrl: string, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await backendReachable(baseUrl, 2_000)) return true;
    await new Promise((r) => setTimeout(r, HEALTH_POLL_MS));
  }
  return false;
}

/**
 * Ensure a locally managed Cognee server is up. Returns true if the server is
 * reachable (either already running, or successfully provisioned + spawned).
 */
export async function ensureLocalServer(
  cfg: CogneePluginConfig,
  logger: Logger,
): Promise<boolean> {
  // 1. Already up? (e.g. a prior boot left it running, or an external process.)
  if (await backendReachable(cfg.baseUrl)) {
    hookLog("managed_server_already_up", { baseUrl: cfg.baseUrl });
    return true;
  }

  // 2. Resolve a Python interpreter. This is the hard floor — without one we
  //    cannot provision or run anything.
  const python = await resolvePython(cfg.server.python);
  if (!python) {
    logger.error(
      { configured: cfg.server.python },
      "no Python interpreter found — cannot start managed cognee server. " +
        "Install Python 3.12+, set config.server.python to its path, or point " +
        "the plugin at a remote server (set mode=cloud + base_url).",
    );
    hookLog("managed_no_python", { configured: cfg.server.python });
    return false;
  }

  // 3. Provision the venv (create + pip install cognee) if needed.
  const ready = await ensureVenv(python, cfg.server, logger);
  if (!ready) return false;

  // 4. Spawn the server, passing the LLM API key through to the server env.
  const llmKey = await resolveLlmApiKey(cfg);
  const serverEnv = { ...cfg.server.env };
  if (llmKey) serverEnv.COGNEE_LLM_API_KEY = llmKey;
  const pid = spawnServer({ ...cfg.server, env: serverEnv }, logger);
  if (!pid) return false;

  // 5. Wait for it to answer the healthcheck.
  const up = await waitForServer(cfg.baseUrl, HEALTH_TIMEOUT_MS);
  if (!up) {
    logger.warn(
      { baseUrl: cfg.baseUrl, log: serverLogPath() },
      "managed cognee server spawned but did not become healthy in time — " +
        "memory hooks will be no-ops until it comes up. Check the server log.",
    );
    hookLog("managed_server_unhealthy", { baseUrl: cfg.baseUrl });
    return false;
  }

  hookLog("managed_server_ready", { baseUrl: cfg.baseUrl, pid });
  return true;
}
