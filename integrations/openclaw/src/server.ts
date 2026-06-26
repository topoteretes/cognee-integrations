import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";
import { runPluginCommandWithTimeout } from "openclaw/plugin-sdk/sandbox";

const COGNEE_PLUGIN_BASE = join(homedir(), ".cognee-plugin");

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
  "COGNEE_VERSION = '1.2.2'",
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
  "        if not uv:",
  "            try:",
  "                uv_env = os.environ.copy()",
  "                uv_env['UV_UNMANAGED_INSTALL'] = UV_DIR",
  "                os.makedirs(UV_DIR, exist_ok=True)",
  "                subprocess.run(",
  "                    ['sh', '-c', 'curl -LsSf https://astral.sh/uv/install.sh | sh'],",
  "                    env=uv_env, check=True, capture_output=True, timeout=120,",
  "                )",
  "                if os.path.exists(UV_BIN): uv = UV_BIN",
  "            except Exception: pass",
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
    if (parsed.port) port = parseInt(parsed.port, 10);
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

/**
 * Poll the Cognee /health endpoint until it returns 200 or the timeout elapses.
 * Returns a Promise so callers can chain deferred work without blocking.
 * Rejects with an Error on timeout.
 */
export async function waitForServerHealth(
  baseUrl: string,
  timeoutMs = 180_000,
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
