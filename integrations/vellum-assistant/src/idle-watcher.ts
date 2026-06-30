/**
 * Idle watcher daemon — persists quiet sessions into Cognee.
 *
 * Ported from idle-watcher.py. A background daemon that:
 *   - Polls the activity file timestamp
 *   - When idle for > IDLE_SECONDS, triggers sync-session-to-graph
 *   - Stops on SIGTERM or watcher.stop sentinel file
 *
 * Launched detached from session-start. Polls
 * `~/.cognee-plugin/vellum-assistant/activity.ts` every POLL_SECONDS.
 * When the last activity is older than IDLE_SECONDS and we haven't
 * bridged since that point, persists the session cache to the graph.
 */

import { existsSync, readFileSync, writeFileSync, unlinkSync, mkdirSync, openSync } from "node:fs";
import { join } from "node:path";
import { spawn } from "bun";

import { hookLog, pluginStateDir, loadConfig } from "./plugin-common.ts";

// ─── Tunables (via env) ───────────────────────────────────────────────────────

const POLL_SECONDS = Number(process.env.COGNEE_IDLE_POLL ?? 10);
const IDLE_SECONDS = Number(process.env.COGNEE_IDLE_THRESHOLD ?? 60);
const IMPROVE_COOLDOWN = Number(process.env.COGNEE_IMPROVE_COOLDOWN ?? 120);

// ─── Paths ────────────────────────────────────────────────────────────────────

function activityPath(): string {
  return join(pluginStateDir(), "activity.ts");
}

function pidfilePath(): string {
  return join(pluginStateDir(), "watcher.pid");
}

function stopfilePath(): string {
  return join(pluginStateDir(), "watcher.stop");
}

function logPath(): string {
  return join(pluginStateDir(), "watcher.log");
}

// ─── State ────────────────────────────────────────────────────────────────────

let shouldStop = false;

// ─── Helpers ──────────────────────────────────────────────────────────────────

function readActivityTs(): number | null {
  if (!existsSync(activityPath())) return null;
  try {
    return parseFloat(readFileSync(activityPath(), "utf-8").trim());
  } catch (err) {
    hookLog("idle_watcher_activity_read_failed", { error: String(err).slice(0, 200) });
    return null;
  }
}

function ownsPidfile(): boolean {
  try {
    return parseInt(readFileSync(pidfilePath(), "utf-8").trim(), 10) === process.pid;
  } catch {
    return false;
  }
}

function installSignalHandlers(): void {
  const handler = (sig: string) => {
    shouldStop = true;
    hookLog("idle_watcher_signal_received", { signal: sig });
  };

  process.on("SIGTERM", () => handler("SIGTERM"));
  process.on("SIGINT", () => handler("SIGINT"));
}

// ─── Bridge (sync to graph) ───────────────────────────────────────────────────

/**
 * Fire one session bridge cycle. Spawns the sync-session-to-graph module
 * as a detached process. Returns true on success.
 */
async function improveOnce(
  sessionId: string,
  dataset: string,
): Promise<boolean> {
  try {
    // Import the sync module and call syncSessionToGraph directly.
    // This keeps everything in-process — no subprocess needed.
    const { syncSessionToGraph } = await import("./sync-session-to-graph.ts");
    const ok = await syncSessionToGraph(false);
    hookLog("idle_watcher_session_bridge_done", {
      session: sessionId,
      dataset,
      via: "http_remember",
      ok,
    });
    return ok;
  } catch (err) {
    hookLog("idle_watcher_bridge_error", { error: String(err).slice(0, 300) });
    return false;
  }
}

// ─── Main loop ────────────────────────────────────────────────────────────────

export interface IdleWatcherConfig {
  session_id: string;
  dataset: string;
  user_id?: string;
  session_key?: string;
  config?: Record<string, unknown>;
}

/**
 * Run the idle watcher main loop.
 *
 * @param config Bootstrap config from the launching hook.
 */
export async function runIdleWatcher(config: IdleWatcherConfig): Promise<void> {
  const sessionId = config.session_id || "";
  const dataset = config.dataset || "agent_sessions";

  if (!sessionId) {
    hookLog("idle_watcher_fatal_no_session_id");
    return;
  }

  // Ensure the plugin state dir exists.
  try {
    mkdirSync(pluginStateDir(), { recursive: true });
  } catch {
    // Best-effort.
  }

  // Write the pidfile.
  try {
    writeFileSync(pidfilePath(), String(process.pid), "utf-8");
  } catch (err) {
    hookLog("idle_watcher_pidfile_write_failed", { error: String(err).slice(0, 200) });
    return;
  }

  // Clear any stale stop sentinel from a prior run.
  try {
    if (existsSync(stopfilePath())) {
      unlinkSync(stopfilePath());
    }
  } catch (err) {
    hookLog("idle_watcher_stopfile_unlink_failed", { error: String(err).slice(0, 200) });
  }

  // Set env vars for the sync module.
  if (config.session_key) {
    process.env.COGNEE_SESSION_KEY = config.session_key;
  }

  installSignalHandlers();

  hookLog("idle_watcher_started", {
    session: sessionId,
    dataset,
    user_id: config.user_id ?? "",
    poll: POLL_SECONDS,
    idle: IDLE_SECONDS,
  });

  let lastImprovedAt = 0.0;
  let exitReason = "loop_complete";
  let bridgeDisabled = false;

  while (!shouldStop) {
    // Check stop conditions.
    if (existsSync(stopfilePath())) {
      hookLog("idle_watcher_stop_sentinel_seen");
      exitReason = "stop_sentinel";
      break;
    }
    if (!ownsPidfile()) {
      hookLog("idle_watcher_pidfile_replaced");
      exitReason = "pidfile_replaced";
      break;
    }

    const now = Date.now() / 1000;
    const ts = readActivityTs();
    if (ts === null) {
      await sleep(POLL_SECONDS * 1000);
      continue;
    }

    const idleFor = now - ts;
    const timeSinceImprove = now - lastImprovedAt;

    if (
      !bridgeDisabled &&
      idleFor >= IDLE_SECONDS &&
      timeSinceImprove >= IMPROVE_COOLDOWN
    ) {
      hookLog("idle_watcher_idle_trigger", { idle_for: Math.round(idleFor * 10) / 10 });
      const ok = await improveOnce(sessionId, dataset);
      if (ok) {
        lastImprovedAt = Date.now() / 1000;
        hookLog("idle_watcher_bridge_done");
        exitReason = "bridge_complete";
        break;
      }
      bridgeDisabled = true;
      hookLog("idle_watcher_bridge_disabled_after_failure");
    }

    await sleep(POLL_SECONDS * 1000);
  }

  if (shouldStop) {
    exitReason = "signal";
  }

  // On shutdown (signal or stop sentinel), do one final bridge if there's
  // been activity since the last improve.
  const ts = readActivityTs();
  if (
    !bridgeDisabled &&
    (exitReason === "signal" || exitReason === "stop_sentinel") &&
    ts !== null &&
    ts > lastImprovedAt
  ) {
    hookLog("idle_watcher_shutdown_trigger", {
      reason: exitReason,
      activity_age: Math.round((Date.now() / 1000 - ts) * 10) / 10,
    });
    const ok = await improveOnce(sessionId, dataset);
    if (ok) {
      lastImprovedAt = Date.now() / 1000;
      hookLog("idle_watcher_shutdown_bridge_done");
    } else {
      hookLog("idle_watcher_shutdown_bridge_failed");
    }
  }

  hookLog("idle_watcher_exiting", { reason: exitReason });

  // Clean up the pidfile.
  try {
    if (ownsPidfile()) {
      unlinkSync(pidfilePath());
    }
  } catch (err) {
    hookLog("idle_watcher_pidfile_unlink_failed", { error: String(err).slice(0, 200) });
  }
}

// ─── Launcher ─────────────────────────────────────────────────────────────────

/**
 * Launch the idle watcher as a detached background process.
 *
 * Called from session-start. The watcher re-execs this module file with
 * the config JSON as an argument.
 *
 * @param config The watcher configuration.
 * @returns The spawned process PID, or 0 on failure.
 */
export function launchIdleWatcher(config: IdleWatcherConfig): number {
  // If a watcher is already alive, kill it so the new one takes over.
  if (existsSync(pidfilePath())) {
    try {
      const pid = parseInt(readFileSync(pidfilePath(), "utf-8").trim(), 10);
      if (pid > 1 && pid !== process.pid) {
        try {
          process.kill(pid, "SIGTERM");
        } catch {
          // Already dead.
        }
      }
    } catch {
      // Best-effort.
    }
  }

  // Clear any stale stop sentinel.
  try {
    if (existsSync(stopfilePath())) {
      unlinkSync(stopfilePath());
    }
  } catch {
    // Best-effort.
  }

  try {
    const env: Record<string, string> = { ...process.env } as Record<string, string>;
    if (config.session_key) {
      env.COGNEE_SESSION_KEY = config.session_key;
    }

    const modulePath = new URL(import.meta.url).pathname;
    const logFd = openSync(logPath(), "a");
    const proc = spawn({
      cmd: ["bun", "run", modulePath, JSON.stringify(config)],
      stdin: "ignore",
      stdout: logFd,
      stderr: logFd,
      env,
      detached: true,
    });

    hookLog("idle_watcher_launched", {
      session: config.session_id,
      dataset: config.dataset,
      pid: proc.pid,
    });

    return proc.pid ?? 0;
  } catch (err) {
    hookLog("idle_watcher_launch_failed", { error: String(err).slice(0, 300) });
    return 0;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ─── CLI entry point ──────────────────────────────────────────────────────────

/**
 * When this module is run directly, parse the config JSON arg and run
 * the watcher loop.
 */
async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args.length < 1) {
    hookLog("idle_watcher_fatal_missing_args");
    process.exit(1);
  }

  try {
    const config = JSON.parse(args[0]) as IdleWatcherConfig;
    // Merge in any config overrides from the bootstrap.
    if (config.config) {
      const cfg = loadConfig();
      const overrides = config.config as Record<string, unknown>;
      if (overrides.base_url) {
        process.env.COGNEE_BASE_URL = String(overrides.base_url);
      }
      if (overrides.dataset) {
        config.dataset = String(overrides.dataset);
      }
    }
    await runIdleWatcher(config);
  } catch (err) {
    hookLog("idle_watcher_fatal_bad_args", { error: String(err).slice(0, 200) });
    process.exit(1);
  }
}

// Run main when this file is the entry point.
if (import.meta.main) {
  main();
}

export default runIdleWatcher;
