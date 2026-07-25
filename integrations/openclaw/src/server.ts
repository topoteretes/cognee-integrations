import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";
import { runPluginCommandWithTimeout } from "openclaw/plugin-sdk/sandbox";

const COGNEE_PLUGIN_BASE = join(homedir(), ".cognee-plugin");
const API_KEY_CACHE_PATH = join(COGNEE_PLUGIN_BASE, "api_key.json");

// Combined install-and-boot script written to ~/.cognee-plugin/ensure_and_boot.py on first use.
// Handles: (1) creating the venv + installing cognee if absent, (2) booting uvicorn.
// Self-daemonizes so runPluginCommandWithTimeout returns in < 1 s regardless of install time.
// Inlining avoids import.meta.url path resolution issues in ts-jest.
const ENSURE_SCRIPT_PATH = join(COGNEE_PLUGIN_BASE, "ensure_and_boot.py");
const ENSURE_SCRIPT_CONTENT = [
  "import subprocess, sys, os, json, time",
  "",
  "BASE = os.path.join(os.path.expanduser('~'), '.cognee-plugin')",
  "VENV_DIR = os.path.join(BASE, 'venv')",
  "_bin = 'Scripts' if os.name == 'nt' else 'bin'",
  "_ext = '.exe' if os.name == 'nt' else ''",
  "VENV_PYTHON = os.path.join(VENV_DIR, _bin, 'python' + _ext)",
  "PIP = os.path.join(VENV_DIR, _bin, 'pip' + _ext)",
  "UV_DIR = os.path.join(BASE, 'uv')",
  "UV_BIN = os.path.join(UV_DIR, 'uv' + _ext)",
  "READY_MARKER = os.path.join(BASE, '.venv-ready.json')",
  "INSTALL_LOCK = os.path.join(BASE, 'venv-install.lock')",
  "COGNEE_VERSION = '1.2.2.dev3'",
  "",
  "# Self-daemonize so the caller returns immediately.",
  "if '--daemon' not in sys.argv:",
  "    port_arg = sys.argv[1] if len(sys.argv) > 1 else '8011'",
  "    p = subprocess.Popen(",
  "        [sys.executable, __file__, port_arg, '--daemon'],",
  "        start_new_session=True, close_fds=True,",
  "        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,",
  "    )",
  "    print(p.pid)",
  "    sys.exit(0)",
  "",
  "PORT = next((a for a in sys.argv[1:] if a != '--daemon'), '8011')",
  "",
  "def pid_alive(pid):",
  "    if pid <= 1: return False",
  "    try: os.kill(pid, 0); return True",
  "    except ProcessLookupError: return False",
  "    except PermissionError: return True",
  "    except Exception: return False",
  "",
  "def acquire_lock(path, stale_secs=720):",
  "    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)",
  "    now = time.time()",
  "    if os.path.exists(path):",
  "        try:",
  "            d = json.loads(open(path).read())",
  "            pid = int(d.get('pid', 0) or 0)",
  "            created = float(d.get('created_at', 0) or 0)",
  "            if pid_alive(pid) and now - created < stale_secs: return False",
  "            os.unlink(path)",
  "        except Exception: pass",
  "    try:",
  "        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)",
  "        with os.fdopen(fd, 'w') as f: json.dump({'pid': os.getpid(), 'created_at': now}, f)",
  "        return True",
  "    except FileExistsError: return False",
  "",
  "def release_lock(path):",
  "    try: os.unlink(path)",
  "    except Exception: pass",
  "",
  "def install_cognee():",
  "    os.makedirs(BASE, exist_ok=True)",
  "    if not acquire_lock(INSTALL_LOCK):",
  "        deadline = time.monotonic() + 720",
  "        while time.monotonic() < deadline:",
  "            if os.path.exists(VENV_PYTHON): return True",
  "            time.sleep(0.5)",
  "        return os.path.exists(VENV_PYTHON)",
  "    try:",
  "        import shutil",
  "        uv = UV_BIN if os.path.exists(UV_BIN) else (shutil.which('uv') or '')",
  "        if uv:",
  "            uv_env = os.environ.copy()",
  "            if not os.path.exists(VENV_PYTHON):",
  "                subprocess.run(",
  "                    [uv, 'venv', VENV_DIR, '--python', '3.12'],",
  "                    env=uv_env, check=True, capture_output=True, timeout=300,",
  "                )",
  "            subprocess.run(",
  "                [uv, 'pip', 'install', '--upgrade', '--python', VENV_PYTHON,",
  "                 f'cognee=={COGNEE_VERSION}'],",
  "                env=uv_env, check=True, capture_output=True, timeout=600,",
  "            )",
  "        elif not os.path.exists(VENV_PYTHON):",
  "            subprocess.run(",
  "                [sys.executable, '-m', 'venv', VENV_DIR],",
  "                check=True, capture_output=True, timeout=120,",
  "            )",
  "            subprocess.run(",
  "                [PIP, 'install', '--upgrade', f'cognee=={COGNEE_VERSION}'],",
  "                check=True, capture_output=True, timeout=600,",
  "            )",
  "        tmp = READY_MARKER + '.tmp'",
  "        with open(tmp, 'w') as f:",
  "            json.dump({'cognee_version': COGNEE_VERSION, 'python': VENV_PYTHON,",
  "                       'updated_at': time.time()}, f)",
  "        os.replace(tmp, READY_MARKER)",
  "        return True",
  "    except Exception: return False",
  "    finally: release_lock(INSTALL_LOCK)",
  "",
  "def boot_server():",
  "    if not os.path.exists(VENV_PYTHON): return",
  "    home = os.path.expanduser('~')",
  "    env = dict(os.environ)",
  "    env['COGNEE_AGENT_MODE'] = 'true'",
  "    env['AUTO_FEEDBACK'] = 'true'",
  "    env['CACHING'] = 'true'",
  "    env['SYSTEM_ROOT_DIRECTORY'] = os.path.join(home, '.cognee', 'system')",
  "    env['DATA_ROOT_DIRECTORY'] = os.path.join(home, '.cognee', 'data')",
  "    env['CACHE_ROOT_DIRECTORY'] = os.path.join(home, '.cognee', 'cache')",
  "    subprocess.Popen(",
  "        [VENV_PYTHON, '-m', 'uvicorn', 'cognee.api.client:app', '--port', PORT],",
  "        env=env, start_new_session=True, close_fds=True,",
  "        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,",
  "    )",
  "",
  "def needs_install():",
  "    if not os.path.exists(VENV_PYTHON): return True",
  "    if os.path.exists(READY_MARKER):",
  "        try:",
  "            d = json.loads(open(READY_MARKER).read())",
  "            if d.get('cognee_version') == COGNEE_VERSION: return False",
  "        except Exception: pass",
  "    return True",
  "",
  "if needs_install():",
  "    install_cognee()",
  "boot_server()",
].join("\n");

// System Python candidates for running ensure_and_boot.py on a cold machine
// (before the plugin venv exists). Only stdlib is needed so any Python 3 works.
const SYSTEM_PYTHON_CANDIDATES = [
  "/usr/bin/python3",          // macOS Xcode CLT + most Linux
  "/usr/local/bin/python3",    // some Linux, older Homebrew
  "/opt/homebrew/bin/python3", // Homebrew on Apple Silicon
];

function findSystemPython(): string {
  for (const p of SYSTEM_PYTHON_CANDIDATES) {
    if (existsSync(p)) return p;
  }
  return "python3"; // PATH fallback
}

type ApiKeyClient = {
  baseUrl: string;
  listApiKeys(): Promise<{ key: string; name?: string }[]>;
  createApiKey(name: string): Promise<{ key: string }>;
};

async function saveApiKeyCache(baseUrl: string, key: string): Promise<void> {
  try {
    await mkdir(COGNEE_PLUGIN_BASE, { recursive: true });
    await writeFile(
      API_KEY_CACHE_PATH,
      JSON.stringify({ base_url: baseUrl, api_key: key, updated_at: new Date().toISOString() }),
      "utf-8",
    );
  } catch { /* best-effort */ }
}

/**
 * Resolve a permanent Cognee API key for this deployment, using the same
 * strategy as the claude-code and codex integrations:
 *   1. COGNEE_API_KEY env
 *   2. Cached key in ~/.cognee-plugin/api_key.json
 *   3. Existing key returned by GET /api/v1/auth/api-keys
 *   4. Mint a new one via POST /api/v1/auth/api-keys and cache it
 *
 * The client's ensureAuth() has already run before this is called, so
 * the HTTP calls go out authenticated. Returns "" if every path fails
 * (e.g. older Cognee that doesn't expose the api-keys endpoints).
 */
export async function resolveOrMintApiKey(
  client: ApiKeyClient,
  logger: { info?: (msg: string) => void; warn?: (msg: string) => void },
): Promise<string> {
  const envKey = (process.env["COGNEE_API_KEY"] ?? "").trim();
  if (envKey) return envKey;

  try {
    const cache = JSON.parse(await readFile(API_KEY_CACHE_PATH, "utf-8")) as Record<string, unknown>;
    const key = typeof cache.api_key === "string" ? cache.api_key.trim() : "";
    if (key) return key;
  } catch { /* cache miss */ }

  try {
    const keys = await client.listApiKeys();
    const key = Array.isArray(keys) ? (keys[0]?.key ?? "").trim() : "";
    if (key) {
      await saveApiKeyCache(client.baseUrl, key);
      return key;
    }
  } catch (e) {
    logger.warn?.(`cognee-openclaw: list API keys failed: ${String(e)}`);
  }

  try {
    const { key } = await client.createApiKey("openclaw-bootstrap");
    const trimmed = (key ?? "").trim();
    if (trimmed) {
      await saveApiKeyCache(client.baseUrl, trimmed);
      logger.info?.("cognee-openclaw: minted new API key");
      return trimmed;
    }
  } catch (e) {
    logger.warn?.(`cognee-openclaw: create API key failed: ${String(e)}`);
  }

  return "";
}

export function isLocalUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    return ["localhost", "127.0.0.1", "0.0.0.0", "::1"].includes(parsed.hostname);
  } catch {
    return false;
  }
}

/**
 * Write ensure_and_boot.py and run it via the plugin sandbox.
 * The script self-daemonizes, so this returns in < 1 s regardless of whether
 * cognee needs to be installed from scratch.
 */
export async function bootServerIfNeeded(
  baseUrl: string,
  logger: { info?: (msg: string) => void; warn?: (msg: string) => void },
): Promise<void> {
  if (!isLocalUrl(baseUrl)) return;

  let port = 8011;
  try {
    const parsed = new URL(baseUrl);
    if (parsed.port) {
      const p = parseInt(parsed.port, 10);
      if (Number.isFinite(p) && p > 0) port = p;
    }
  } catch { /* keep default */ }

  const python = findSystemPython();
  await mkdir(COGNEE_PLUGIN_BASE, { recursive: true });
  await writeFile(ENSURE_SCRIPT_PATH, ENSURE_SCRIPT_CONTENT, "utf-8");

  const result = await runPluginCommandWithTimeout({
    argv: [python, ENSURE_SCRIPT_PATH, String(port)],
    timeoutMs: 5_000,
  });
  if (result.code !== 0) {
    logger.warn?.(`cognee-openclaw: boot script exited ${result.code}: ${result.stderr}`);
  }
}

// ---------------------------------------------------------------------------
// Exit-watcher: detached Python process that deregisters an agent session
// when the OpenClaw gateway process dies (by any means: Ctrl+C, SIGKILL,
// crash, clean exit). One watcher per registration, all watching the same
// gateway PID. State lives in ~/.openclaw/cognee/ separate from the shared
// Cognee venv infrastructure in ~/.cognee-plugin/.
// ---------------------------------------------------------------------------

const OPENCLAW_STATE_DIR = join(homedir(), ".openclaw", "cognee");
const EXIT_WATCHER_SCRIPT_PATH = join(OPENCLAW_STATE_DIR, "exit-watcher.py");
const EXIT_WATCHERS_DIR = join(OPENCLAW_STATE_DIR, "exit-watchers");

const EXIT_WATCHER_CONTENT = [
  "import json, os, sys, time, urllib.request",
  "",
  "POLL = 2.0",
  "LOG_PATH = os.path.join(os.path.expanduser('~'), '.openclaw', 'cognee', 'exit-watcher.log')",
  "",
  "def log(msg):",
  "    try:",
  "        with open(LOG_PATH, 'a') as f:",
  "            f.write(time.strftime('%Y-%m-%dT%H:%M:%S') + ' ' + str(msg) + '\\n')",
  "    except Exception: pass",
  "",
  "def pid_alive(pid):",
  "    if pid <= 1: return False",
  "    try: os.kill(pid, 0); return True",
  "    except ProcessLookupError: return False",
  "    except PermissionError: return True",
  "    except Exception: return False",
  "",
  "def owns_pidfile(path):",
  "    try: return int(open(path).read().strip()) == os.getpid()",
  "    except Exception: return False",
  "",
  "def post_json(base_url, path, payload, api_key, timeout):",
  "    url = base_url.rstrip('/') + path",
  "    data = json.dumps(payload).encode()",
  "    req = urllib.request.Request(url, data=data, method='POST',",
  "          headers={'Content-Type': 'application/json'})",
  "    # X-Api-Key only: an API key is not a JWT, and a bogus Bearer can be",
  "    # rejected by JWT-validating servers before the key is considered.",
  "    if api_key:",
  "        req.add_header('X-Api-Key', api_key)",
  "    with urllib.request.urlopen(req, timeout=timeout) as r:",
  "        return r.read()",
  "",
  "def bridge_session(base_url, dataset_name, session_id, api_key):",
  "    # Bridge the server-side session cache into the permanent graph BEFORE",
  "    # unregistering: unregister may drop activeAgents to 0, and in",
  "    # COGNEE_AGENT_MODE the server then shuts down mid-pipeline.",
  "    # run_in_background=false so the call returns only when the bridge is done.",
  "    try:",
  "        body = post_json(base_url, '/api/v1/improve',",
  "            {'dataset_name': dataset_name, 'session_ids': [session_id],",
  "             'run_in_background': False}, api_key, 120)",
  "        log(f'improve ok session={session_id} body={body.decode()[:200]}')",
  "    except Exception as e:",
  "        log(f'improve error session={session_id} type={type(e).__name__} err={e}')",
  "",
  "def deregister(base_url, name, api_key):",
  "    try:",
  "        body = post_json(base_url, '/api/v1/agents/unregister',",
  "            {'agent_session_name': name}, api_key, 10)",
  "        log(f'deregister ok name={name} body={body.decode()[:200]}')",
  "    except Exception as e:",
  "        log(f'deregister error name={name} type={type(e).__name__} err={e}')",
  "",
  "if '--daemon' not in sys.argv:",
  "    import subprocess",
  "    subprocess.Popen([sys.executable, __file__, sys.argv[1], '--daemon'],",
  "        start_new_session=True, close_fds=True,",
  "        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
  "    sys.exit(0)",
  "",
  "try: a = json.loads(next(x for x in sys.argv[1:] if x != '--daemon'))",
  "except Exception as e: log(f'arg parse error: {e}'); sys.exit(0)",
  "",
  "gw_pid = int(a.get('gateway_pid', 0))",
  "name = str(a.get('agent_session_name', ''))",
  "base_url = str(a.get('base_url', 'http://localhost:8011'))",
  "api_key = str(a.get('api_key', '') or '')",
  "pidfile = str(a.get('pidfile', ''))",
  "dataset_name = str(a.get('dataset_name', '') or '')",
  "cognee_session_id = str(a.get('cognee_session_id', '') or '')",
  "",
  "if not gw_pid or not name or not pidfile:",
  "    log(f'bad args gw_pid={gw_pid} name={name} pidfile={pidfile}'); sys.exit(0)",
  "",
  "try:",
  "    os.makedirs(os.path.dirname(pidfile) or '.', exist_ok=True)",
  "    open(pidfile, 'w').write(str(os.getpid()))",
  "except Exception as e:",
  "    log(f'pidfile write error: {e}'); sys.exit(0)",
  "",
  "log(f'started pid={os.getpid()} gw_pid={gw_pid} name={name}')",
  "",
  "while owns_pidfile(pidfile) and pid_alive(gw_pid):",
  "    time.sleep(POLL)",
  "",
  "if owns_pidfile(pidfile):",
  "    log(f'gateway dead, deregistering name={name}')",
  "    if dataset_name and cognee_session_id:",
  "        bridge_session(base_url, dataset_name, cognee_session_id, api_key)",
  "    deregister(base_url, name, api_key)",
  "    try: os.unlink(pidfile)",
  "    except Exception: pass",
  "else:",
  "    log(f'pidfile gone (clean exit) name={name}')",
].join("\n");

/** Returns the pidfile path for an exit-watcher tracking the given session name. */
export function exitWatcherPidfilePath(agentSessionName: string): string {
  const safe = agentSessionName.replace(/[^a-zA-Z0-9_-]/g, "-").slice(0, 200);
  return join(EXIT_WATCHERS_DIR, `${safe}.pid`);
}

/**
 * Spawn a detached exit-watcher that fires when the gateway process
 * (gatewayPid) exits for any reason. If datasetName + cogneeSessionId are
 * given, it first bridges that session's cache into the graph via
 * /api/v1/improve, then calls /api/v1/agents/unregister for agentSessionName.
 * Returns immediately — the actual watcher runs as a separate OS process.
 * Signal clean deregistration by deleting the pidfile (exitWatcherPidfilePath);
 * the watcher detects the missing pidfile and self-exits without making an HTTP call.
 */
export async function spawnExitWatcher(params: {
  gatewayPid: number;
  agentSessionName: string;
  baseUrl: string;
  apiKey?: string;
  pidfilePath: string;
  datasetName?: string;
  cogneeSessionId?: string;
  logger: { warn?: (msg: string) => void };
}): Promise<void> {
  try {
    await mkdir(EXIT_WATCHERS_DIR, { recursive: true });
    await writeFile(EXIT_WATCHER_SCRIPT_PATH, EXIT_WATCHER_CONTENT, "utf-8");
    const args = JSON.stringify({
      gateway_pid: params.gatewayPid,
      agent_session_name: params.agentSessionName,
      base_url: params.baseUrl,
      api_key: params.apiKey ?? "",
      pidfile: params.pidfilePath,
      dataset_name: params.datasetName ?? "",
      cognee_session_id: params.cogneeSessionId ?? "",
    });
    const python = findSystemPython();
    const result = await runPluginCommandWithTimeout({
      argv: [python, EXIT_WATCHER_SCRIPT_PATH, args],
      timeoutMs: 5_000,
    });
    if (result.code !== 0) {
      params.logger.warn?.(`cognee-openclaw: exit-watcher spawn failed (exit ${result.code}): ${result.stderr}`);
    }
  } catch (e) {
    params.logger.warn?.(`cognee-openclaw: exit-watcher spawn error: ${String(e)}`);
  }
}

/**
 * Poll the Cognee /health endpoint until it returns 200 or the timeout elapses.
 * Returns a Promise so callers can chain deferred work without blocking.
 * Rejects with an Error on timeout.
 */
export async function waitForServerHealth(
  baseUrl: string,
  timeoutMs = 600_000,
): Promise<void> {
  const healthUrl = `${baseUrl.replace(/\/$/, "")}/health`;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise<void>((r) => setTimeout(r, 2_000));
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 2_000);
      const resp = await fetch(healthUrl, { signal: ctrl.signal });
      clearTimeout(t);
      if (resp.ok) return;
    } catch { /* not ready yet */ }
  }
  throw new Error(`Cognee server not healthy after ${Math.round(timeoutMs / 1000)}s`);
}
