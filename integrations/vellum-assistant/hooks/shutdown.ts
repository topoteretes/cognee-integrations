/**
 * shutdown hook — fires once when the assistant tears down the plugin
 * (process exit, unload).
 *
 *   1. Triggers a final session-to-graph sync (with unregister)
 *   2. Clears the server-ready marker
 */

import type { ShutdownContext } from "@vellumai/plugin-api";

import {
  getSessionKey,
  hookLog,
  clearServerReady,
  loadConfig,
  readServerPid,
  clearServerPid,
} from "../src/plugin-common.ts";
import { syncSessionToGraph } from "../src/sync-session-to-graph.ts";

/**
 * Tear down a plugin-spawned (managed) Cognee server. Only the process the
 * plugin started is killed — a remote or externally managed server is left
 * alone (no PID is recorded for those).
 */
function stopManagedServer(): void {
  const cfg = loadConfig();
  if (!cfg.managed) return;

  const pid = readServerPid();
  if (!pid) return;

  try {
    process.kill(pid, "SIGTERM");
    hookLog("managed_server_stopped", { pid });
  } catch (err) {
    // Already gone, or not ours to kill — best-effort.
    hookLog("managed_server_stop_failed", { pid, error: String(err).slice(0, 200) });
  }
  clearServerPid();
}

export default async function shutdown(_ctx: ShutdownContext): Promise<void> {
  // The session key should still be in the env from earlier hooks.
  const sessionKey = getSessionKey();

  // 1. Final graph sync with unregister (only if a session ran).
  if (sessionKey) {
    try {
      await syncSessionToGraph(true);
    } catch (err) {
      hookLog("shutdown_sync_failed", { error: String(err).slice(0, 200) });
    }
  }

  // 2. Stop the managed server (if we own one).
  stopManagedServer();

  // 3. Clear the server-ready marker.
  clearServerReady();

  hookLog("shutdown_complete");
}
