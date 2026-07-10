/**
 * Bridge session cache entries into the permanent knowledge graph.
 *
 * Ported from sync-session-to-graph.py. Responsibilities:
 *   1. Load the bridge cache for the session
 *   2. Post QA and trace documents to the permanent graph via doRemember()
 *   3. Dedup by content hash (via isBridgePosted / markBridgePosted)
 *   4. Unregister the agent connection if --session-end flag
 *
 * Best-effort: never throws from hooks.
 */

import { existsSync, writeFileSync, unlinkSync, statSync, readFileSync, readdirSync, mkdirSync, openSync, writeSync, closeSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";

import {
  loadConfig,
  hookLog,
  getSessionKey,
  resolveSessionId,
  sanitizeSessionKey,
  resolveHttpEndpoint,
  resolveApiKey,
  pluginStateDir,
  formatBridgeDocument,
  isBridgePosted,
  markBridgePosted,
  cacheApiKey,
  getConnUuid,
} from "./plugin-common.ts";
import {
  doRemember,
  unregisterAgent,
  backendReachable,
  UNREACHABLE,
} from "./cognee-client.ts";

// ─── Constants ────────────────────────────────────────────────────────────────

const DETACHED_RETRIES_DEFAULT = 3;
const DETACHED_RETRY_DELAY_DEFAULT = 10.0; // seconds
const FINAL_SYNC_ONCE_TTL_SECONDS = 3600;

// ─── Idle watcher stop ────────────────────────────────────────────────────────

function watcherPidPath(): string {
  return join(pluginStateDir(), "watcher.pid");
}

function watcherStopPath(): string {
  return join(pluginStateDir(), "watcher.stop");
}

/**
 * Signal the idle watcher to exit and drop its pidfile.
 * Uses both a sentinel file and SIGTERM (fast path).
 */
export function stopIdleWatcher(): void {
  try {
    writeFileSync(watcherStopPath(), "stop", "utf-8");
  } catch (err) {
    hookLog("watcher_stop_write_failed", { error: String(err).slice(0, 200) });
  }
  // SIGTERM the watcher if we can find its pid.
  try {
    if (existsSync(watcherPidPath())) {
      const pid = parseInt(readFileSync(watcherPidPath(), "utf-8").trim(), 10);
      if (pid > 1) {
        try {
          process.kill(pid, "SIGTERM");
        } catch {
          // Already dead or no permission.
        }
      }
    }
  } catch {
    // Best-effort.
  }
}

// ─── Final-sync-once dedup ────────────────────────────────────────────────────

function finalSyncOnceDir(): string {
  return join(pluginStateDir(), "final-sync-once");
}

function pruneFinalSyncMarkers(): void {
  try {
    const dir = finalSyncOnceDir();
    if (!existsSync(dir)) return;
    const now = Date.now() / 1000;
    let removed = 0;
    for (const name of readdirSync(dir) as string[]) {
      if (!name.endsWith(".done")) continue;
      const p = join(dir, name);
      try {
        const age = now - statSync(p).mtimeMs / 1000;
        if (age > FINAL_SYNC_ONCE_TTL_SECONDS) {
          unlinkSync(p);
          removed++;
        }
      } catch {
        continue;
      }
    }
    if (removed) {
      hookLog("final_sync_once_pruned", { removed, ttl: FINAL_SYNC_ONCE_TTL_SECONDS });
    }
  } catch (err) {
    hookLog("final_sync_once_prune_failed", { error: String(err).slice(0, 200) });
  }
}

/**
 * Allow exactly one detached final sync worker per session.
 * Returns true if this caller should proceed.
 */
function claimFinalSyncOnce(): boolean {
  pruneFinalSyncMarkers();

  const token = getSessionKey() || process.env.COGNEE_SYNC_SESSION_ID || "";
  if (!token) {
    // No stable identity — don't risk skipping final sync.
    hookLog("final_sync_once_no_token");
    return true;
  }

  const digest = createHash("sha1").update(token).digest("hex");
  const marker = join(finalSyncOnceDir(), `${digest}.done`);
  try {
    mkdirSync(finalSyncOnceDir(), { recursive: true });
    // O_CREAT | O_EXCL equivalent: try to open exclusively.
    const fd = openSync(marker, "wx");
    writeSync(fd, token);
    closeSync(fd);
    hookLog("final_sync_once_claimed", { marker });
    return true;
  } catch (err: any) {
    if (err?.code === "EEXIST") {
      hookLog("final_sync_once_already_claimed", { marker });
      return false;
    }
    // On marker failure, prefer proceeding to avoid data loss.
    hookLog("final_sync_once_claim_failed", { error: String(err).slice(0, 200) });
    return true;
  }
}

// ─── Session resolution ───────────────────────────────────────────────────────

interface ResolvedSession {
  sessionId: string;
  dataset: string;
  sessionKey: string;
  agentSessionName: string;
}

function resolveSyncSession(): ResolvedSession {
  const cfg = loadConfig();
  let sessionKey = getSessionKey();
  if (!sessionKey) {
    const envKey = process.env.COGNEE_SYNC_SESSION_ID ?? "";
    if (envKey) sessionKey = sanitizeSessionKey(envKey);
  }
  const sessionId =
    process.env.COGNEE_SYNC_SESSION_ID ||
    resolveSessionId(sessionKey, cfg.agentName);
  const dataset = process.env.COGNEE_SYNC_DATASET || cfg.dataset;
  const agentSessionName =
    process.env.COGNEE_AGENT_SESSION_NAME ||
    getConnUuid(sessionKey) ||
    sessionKey ||
    sessionId;

  return { sessionId, dataset, sessionKey, agentSessionName };
}

// ─── Core sync logic ──────────────────────────────────────────────────────────

/**
 * Bridge the session cache into the permanent graph.
 *
 * Posts QA and trace documents via doRemember(), deduplicating by content
 * hash so re-runs don't duplicate data.
 *
 * @param unregisterOnFinish If true, unregister the agent connection after syncing.
 * @returns True if the sync completed (even with partial writes).
 */
export async function syncSessionToGraph(
  unregisterOnFinish = false,
): Promise<boolean> {
  const { sessionId, dataset, sessionKey, agentSessionName } = resolveSyncSession();

  if (!sessionId) {
    hookLog("sync_no_target_sessions", { dataset });
    return false;
  }

  hookLog("sync_start", { session: sessionId, dataset, unregisterOnFinish });

  const { baseUrl } = resolveHttpEndpoint();
  const apiKey = resolveApiKey(baseUrl);
  if (!apiKey) {
    hookLog("sync_no_api_key", { baseUrl });
    return false;
  }

  // Check backend reachability.
  const reachable = await backendReachable(baseUrl);
  if (!reachable) {
    hookLog("sync_backend_unreachable", { baseUrl });
    return false;
  }

  // Format the bridge documents from the cache.
  const [qaDoc, traceDoc] = formatBridgeDocument(sessionId, dataset);

  let wrote = false;

  // Post QA document if it has content and hasn't been posted yet.
  if (qaDoc) {
    if (!isBridgePosted(sessionId, dataset, "qa", qaDoc)) {
      try {
        const result = await doRemember(baseUrl, apiKey, qaDoc, dataset, "qa");
        if (result !== UNREACHABLE && (result as Record<string, unknown>)?.ok) {
          markBridgePosted(sessionId, dataset, "qa", qaDoc);
          wrote = true;
          hookLog("sync_bridge_qa_done", { session: sessionId, dataset });
        } else {
          hookLog("sync_bridge_qa_failed", {
            session: sessionId,
            result: String(JSON.stringify(result)).slice(0, 200),
          });
        }
      } catch (err) {
        hookLog("sync_bridge_qa_error", { error: String(err).slice(0, 200) });
      }
    } else {
      hookLog("sync_bridge_qa_dedup", { session: sessionId });
    }
  }

  // Post trace document if it has content and hasn't been posted yet.
  if (traceDoc) {
    if (!isBridgePosted(sessionId, dataset, "trace", traceDoc)) {
      try {
        const result = await doRemember(baseUrl, apiKey, traceDoc, dataset, "trace");
        if (result !== UNREACHABLE && (result as Record<string, unknown>)?.ok) {
          markBridgePosted(sessionId, dataset, "trace", traceDoc);
          wrote = true;
          hookLog("sync_bridge_trace_done", { session: sessionId, dataset });
        } else {
          hookLog("sync_bridge_trace_failed", {
            session: sessionId,
            result: String(JSON.stringify(result)).slice(0, 200),
          });
        }
      } catch (err) {
        hookLog("sync_bridge_trace_error", { error: String(err).slice(0, 200) });
      }
    } else {
      hookLog("sync_bridge_trace_dedup", { session: sessionId });
    }
  }

  // Unregister the agent connection if requested.
  if (unregisterOnFinish) {
    const unregisterName = agentSessionName.trim();
    if (unregisterName) {
      try {
        const ok = await unregisterAgent(baseUrl, apiKey, unregisterName);
        hookLog("agent_unregister_result", {
          session: sessionId,
          dataset,
          agent_session_name: unregisterName,
          ok,
        });
      } catch (err) {
        hookLog("agent_unregister_error", { error: String(err).slice(0, 200) });
      }
    } else {
      hookLog("agent_unregister_skipped_no_session_name", { session: sessionId });
    }
  }

  hookLog("sync_bridge_done", {
    session: sessionId,
    dataset,
    via: "http_remember",
    wrote,
  });

  return true;
}

// ─── Session-end entry point ──────────────────────────────────────────────────

/**
 * Run the full session-end sync sequence:
 *   1. Stop the idle watcher
 *   2. Claim the final-sync-once dedup marker
 *   3. Sync the session cache to the graph (with retries)
 *   4. Unregister the agent connection
 *
 * @param sessionEnd If true, stop the watcher and unregister on finish.
 * @returns True if the sync succeeded.
 */
export async function runSync(sessionEnd = false): Promise<boolean> {
  if (sessionEnd) {
    stopIdleWatcher();
  }

  // For session-end, use the detached final-sync dedup.
  if (sessionEnd) {
    // Optional start delay (set by the exit watcher or session-end hook).
    const delayRaw = process.env.COGNEE_SYNC_START_DELAY;
    if (delayRaw) {
      const delay = Number(delayRaw);
      if (Number.isFinite(delay) && delay > 0) {
        hookLog("sync_start_delayed", { seconds: delay });
        await sleep(delay * 1000);
      }
    }
    if (!claimFinalSyncOnce()) {
      hookLog("sync_detached_skipped_duplicate");
      return false;
    }
  }

  const unregisterOnFinish =
    sessionEnd &&
    ["1", "true", "yes"].includes(
      (process.env.COGNEE_UNREGISTER_ON_FINISH ?? "").toLowerCase(),
    );

  const attempts = sessionEnd
    ? parseInt(process.env.COGNEE_SYNC_RETRIES ?? String(DETACHED_RETRIES_DEFAULT), 10)
    : 1;
  const retryDelay = sessionEnd
    ? Number(process.env.COGNEE_SYNC_RETRY_DELAY ?? DETACHED_RETRY_DELAY_DEFAULT)
    : 0;

  for (let attempt = 1; attempt <= Math.max(1, attempts); attempt++) {
    try {
      const ok = await syncSessionToGraph(unregisterOnFinish);
      if (ok) return true;
      // syncSessionToGraph returns false for config issues, not transient errors.
      // But we still retry on session-end for robustness.
    } catch (err) {
      hookLog("sync_failed", {
        attempt,
        attempts,
        error: String(err).slice(0, 300),
      });
    }
    if (attempt < attempts) {
      hookLog("sync_retry_scheduled", { attempt: attempt + 1, delay: retryDelay });
      await sleep(retryDelay * 1000);
    }
  }

  return false;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export default syncSessionToGraph;
