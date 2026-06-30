/**
 * Run Cognee graph sync after the owning host process exits.
 *
 * Ported from exit-watcher.py. A background process that:
 *   - Polls whether the parent PID is alive
 *   - When parent exits, spawns the detached final sync worker
 *   - Uses Bun.spawn with start_new_session equivalent
 *
 * The host (Vellum Assistant) may not invoke plugin SessionEnd on normal
 * shutdown. The init/session-start hook launches this watcher with the
 * parent PID. The watcher does nothing while the host is alive; once that
 * PID disappears, it starts the normal detached graph sync worker and exits.
 */

import { existsSync, readFileSync, writeFileSync, unlinkSync, mkdirSync, readdirSync, openSync } from "node:fs";
import { join } from "node:path";
import { spawn } from "bun";

import { hookLog, pluginStateDir } from "./plugin-common.ts";

// ─── Constants ────────────────────────────────────────────────────────────────

const POLL_SECONDS = 2.0;
const SYNC_START_DELAY = 2.0;

function exitWatchersDir(): string {
  return join(pluginStateDir(), "exit-watchers");
}

function logPath(): string {
  return join(pluginStateDir(), "exit-watcher.log");
}

function defaultPidfilePath(): string {
  return join(pluginStateDir(), "exit-watcher.pid");
}

// ─── PID liveness ─────────────────────────────────────────────────────────────

function pidAlive(pid: number): boolean {
  if (pid <= 1) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err: any) {
    if (err?.code === "ESRCH") return false; // ProcessLookupError
    if (err?.code === "EPERM") return true; // PermissionError — alive but not ours
    hookLog("exit_watcher_pid_check_failed", { pid, error: String(err).slice(0, 200) });
    return false;
  }
}

function ownsPidfile(pidfile: string): boolean {
  try {
    return parseInt(readFileSync(pidfile, "utf-8").trim(), 10) === process.pid;
  } catch {
    return false;
  }
}

// ─── Detached sync spawn ──────────────────────────────────────────────────────

/**
 * Spawn the detached final sync worker (sync-session-to-graph --session-end).
 *
 * Uses Bun.spawn with a new session group so it survives the watcher exiting.
 */
export function spawnDetachedSync(params: {
  sessionId: string;
  dataset: string;
  sessionKey?: string;
  agentSessionName?: string;
  apiKey?: string;
  serviceUrl?: string;
}): void {
  const {
    sessionId,
    dataset,
    sessionKey = "",
    agentSessionName = "",
    apiKey = "",
    serviceUrl = "",
  } = params;

  try {
    const env: Record<string, string> = { ...process.env } as Record<string, string>;
    if (!env.COGNEE_SYNC_START_DELAY) {
      env.COGNEE_SYNC_START_DELAY = String(SYNC_START_DELAY);
    }
    env.COGNEE_UNREGISTER_ON_FINISH = "1";
    if (sessionId) env.COGNEE_SYNC_SESSION_ID = sessionId;
    if (dataset) env.COGNEE_SYNC_DATASET = dataset;
    if (sessionKey) env.COGNEE_SESSION_KEY = sessionKey;
    if (agentSessionName) env.COGNEE_AGENT_SESSION_NAME = agentSessionName;
    if (apiKey) env.COGNEE_API_KEY = apiKey;
    if (serviceUrl) env.COGNEE_BASE_URL = serviceUrl;

    // Resolve the sync-session-to-graph module path.
    // We re-exec this module file with the --detached-final flag, which
    // triggers the runSync(true) path.
    const modulePath = new URL(import.meta.url).pathname;
    const proc = spawn({
      cmd: ["bun", "run", modulePath, "--detached-final"],
      cwd: process.cwd(),
      stdin: "ignore",
      stdout: "ignore",
      stderr: "ignore",
      env,
      // start_new_session equivalent — detached so it survives the watcher.
      detached: true,
    });

    hookLog("exit_sync_deferred", { session: sessionId, dataset, pid: proc.pid });
  } catch (err) {
    hookLog("exit_sync_detach_failed", { error: String(err).slice(0, 300) });
  }
}

// ─── Watcher entry point ──────────────────────────────────────────────────────

export interface ExitWatcherBootstrap {
  parent_pid: number;
  session_id: string;
  dataset: string;
  session_key?: string;
  agent_session_name?: string;
  api_key?: string;
  base_url?: string;
  pidfile?: string;
}

/**
 * Run the exit watcher loop. Polls the parent PID until it exits,
 * then spawns the detached final sync worker.
 *
 * This is the background daemon body. It blocks until the parent exits.
 */
export async function runExitWatcher(bootstrap: ExitWatcherBootstrap): Promise<void> {
  const parentPid = bootstrap.parent_pid || 0;
  const sessionId = bootstrap.session_id || "";
  const dataset = bootstrap.dataset || "agent_sessions";
  const sessionKey = bootstrap.session_key || "";
  const agentSessionName = bootstrap.agent_session_name || "";
  const apiKey = bootstrap.api_key || "";
  const serviceUrl = bootstrap.base_url || "";
  const pidfileRaw = bootstrap.pidfile?.trim() || "";
  const pidfile = pidfileRaw || defaultPidfilePath();

  if (!parentPid) {
    hookLog("exit_watcher_fatal_no_parent_pid", { session: sessionId, dataset });
    return;
  }

  // Ensure directories exist.
  try {
    mkdirSync(pluginStateDir(), { recursive: true });
    mkdirSync(exitWatchersDir(), { recursive: true });
  } catch {
    // Best-effort.
  }

  // Check for an already-running watcher for this parent.
  try {
    if (existsSync(pidfile)) {
      const existing = parseInt(readFileSync(pidfile, "utf-8").trim(), 10);
      if (pidAlive(existing)) {
        hookLog("exit_watcher_already_running", {
          parent_pid: parentPid,
          session: sessionId,
          existing_pid: existing,
        });
        return;
      }
    }
    writeFileSync(pidfile, String(process.pid), "utf-8");
  } catch (err) {
    hookLog("exit_watcher_pidfile_write_failed", { pidfile, error: String(err).slice(0, 200) });
    return;
  }

  hookLog("exit_watcher_started", {
    parent_pid: parentPid,
    session: sessionId,
    dataset,
    pidfile,
  });

  // Poll until the parent exits or we lose pidfile ownership.
  while (ownsPidfile(pidfile) && pidAlive(parentPid)) {
    await sleep(POLL_SECONDS * 1000);
  }

  if (!ownsPidfile(pidfile)) {
    hookLog("exit_watcher_pidfile_replaced", { parent_pid: parentPid, pidfile });
    return;
  }

  hookLog("exit_watcher_parent_exited", { parent_pid: parentPid, session: sessionId, dataset });

  // Spawn the detached final sync worker.
  spawnDetachedSync({
    sessionId,
    dataset,
    sessionKey,
    agentSessionName,
    apiKey,
    serviceUrl,
  });

  // Clean up the pidfile.
  try {
    if (ownsPidfile(pidfile)) {
      unlinkSync(pidfile);
    }
  } catch (err) {
    hookLog("exit_watcher_pidfile_unlink_failed", { pidfile, error: String(err).slice(0, 200) });
  }

  hookLog("exit_watcher_exiting", { parent_pid: parentPid, pidfile });
}

/**
 * Launch the exit watcher as a detached background process.
 *
 * Called from session-start. The watcher re-execs this module file with
 * the bootstrap JSON as an argument.
 *
 * @param bootstrap The watcher configuration.
 * @returns The spawned process PID, or 0 on failure.
 */
export function launchExitWatcher(bootstrap: ExitWatcherBootstrap): number {
  const pidfileRaw = bootstrap.pidfile?.trim() || "";
  const pidfile = pidfileRaw || join(exitWatchersDir(), `${bootstrap.parent_pid}.pid`);

  // Prune stale watcher pidfiles.
  try {
    if (existsSync(exitWatchersDir())) {
      for (const name of readdirSync(exitWatchersDir()) as string[]) {
        if (!name.endsWith(".pid")) continue;
        const p = join(exitWatchersDir(), name);
        try {
          const pid = parseInt(readFileSync(p, "utf-8").trim(), 10);
          if (!pidAlive(pid)) {
            unlinkSync(p);
          }
        } catch {
          continue;
        }
      }
    }
  } catch (err) {
    hookLog("exit_watcher_prune_failed", { error: String(err).slice(0, 200) });
  }

  // Check if a watcher is already running for this parent.
  try {
    if (existsSync(pidfile)) {
      const existing = parseInt(readFileSync(pidfile, "utf-8").trim(), 10);
      if (pidAlive(existing)) {
        hookLog("exit_watcher_already_running", {
          parent_pid: bootstrap.parent_pid,
          pidfile,
        });
        return 0;
      }
    }
  } catch {
    // Fall through.
  }

  // Update the bootstrap with the resolved pidfile path.
  const fullBootstrap = { ...bootstrap, pidfile };

  try {
    const env: Record<string, string> = { ...process.env } as Record<string, string>;
    if (bootstrap.session_key) {
      env.COGNEE_SESSION_KEY = bootstrap.session_key;
    }

    const modulePath = new URL(import.meta.url).pathname;
    const logFd = openSync(logPath(), "a");
    const proc = spawn({
      cmd: ["bun", "run", modulePath, JSON.stringify(fullBootstrap)],
      stdin: "ignore",
      stdout: logFd,
      stderr: logFd,
      env,
      detached: true,
    });

    hookLog("exit_watcher_started", {
      parent_pid: bootstrap.parent_pid,
      session: bootstrap.session_id,
      dataset: bootstrap.dataset,
      pidfile,
      pid: proc.pid,
    });

    return proc.pid ?? 0;
  } catch (err) {
    hookLog("exit_watcher_launch_failed", { error: String(err).slice(0, 300) });
    return 0;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ─── CLI entry point ──────────────────────────────────────────────────────────

/**
 * When this module is run directly (e.g. `bun run exit-watcher.ts <bootstrap>`),
 * parse the bootstrap JSON arg and run the watcher loop.
 *
 * Also handles the `--detached-final` arg from spawnDetachedSync, which
 * delegates to the sync module.
 */
async function main(): Promise<void> {
  const args = process.argv.slice(2);

  // --detached-final: delegate to sync-session-to-graph's runSync(true).
  if (args.includes("--detached-final")) {
    try {
      const { runSync } = await import("./sync-session-to-graph.ts");
      await runSync(true);
    } catch (err) {
      hookLog("exit_watcher_detached_sync_error", { error: String(err).slice(0, 300) });
    }
    return;
  }

  // Normal watcher mode: bootstrap JSON is the first arg.
  if (args.length < 1) {
    hookLog("exit_watcher_fatal_missing_args");
    return;
  }

  try {
    const bootstrap = JSON.parse(args[0]) as ExitWatcherBootstrap;
    await runExitWatcher(bootstrap);
  } catch (err) {
    hookLog("exit_watcher_fatal_bad_args", { error: String(err).slice(0, 200) });
  }
}

// Run main when this file is the entry point.
if (import.meta.main) {
  main();
}

export default runExitWatcher;
