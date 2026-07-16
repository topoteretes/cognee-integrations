/**
 * Initialize Cognee memory at session start.
 *
 * Ported from session-start.py. The Python version handled a lot of server
 * bootstrapping, venv management, and detached workers. In the TypeScript
 * port the heavy server lifecycle is owned by the host (Vellum Assistant
 * or an externally-managed Cognee server), so this module focuses on:
 *
 *   1. Load config (file + env vars)
 *   2. Compute the session ID from the conversationId
 *   3. Ensure the Cognee backend is reachable
 *   4. Resolve or mint an API key (env → cached → mint from local server)
 *   5. Register the session as an active agent connection
 *   6. Ensure the dataset exists
 *   7. Return a system message + additional context for injection
 *
 * Best-effort: never throws from hooks.
 */

import { existsSync } from "node:fs";
import { join } from "node:path";

import {
  loadConfig,
  saveConfig,
  resolveSessionId,
  sanitizeSessionKey,
  getSessionKey,
  hookLog,
  touchActivity,
  markServerReady,
  isServerReady,
  cacheApiKey,
  resolveApiKey,
  loadCachedApiKey,
  isLocalUrl,
  memoryPreferenceSteer,
  pluginStateDir,
  resolveHttpEndpoint,
} from "./plugin-common.ts";
import {
  backendReachable,
  ensureDataset,
  registerAgent,
  resolveAgentConnection,
} from "./cognee-client.ts";

// ─── Types ───────────────────────────────────────────────────────────────────

export interface SessionStartResult {
  /** System message injected into the model's context. */
  systemMessage: string;
  /** Additional context injected (not shown to user). */
  additionalContext: string;
  /** Whether the backend was reachable at start time. */
  ready: boolean;
  /** Resolved Cognee session ID. */
  sessionId: string;
  /** Resolved dataset name. */
  dataset: string;
}

// ─── API key minting (local mode) ─────────────────────────────────────────────

/**
 * Mint an API key from the local Cognee server's default user.
 *
 * Mirrors `_login_default_user_for_owner_api_key` in the Python script:
 *   1. POST /api/v1/auth/login with default credentials → JWT
 *   2. GET  /api/v1/auth/api-keys → reuse first key if one exists
 *   3. POST /api/v1/auth/api-keys → mint a new key if none found
 *
 * Only used when no key is in env or cache and the server is local.
 */
export async function mintApiKey(baseUrl: string, email = "", password = ""): Promise<string> {
  const base = baseUrl.replace(/\/+$/, "");

  try {
    // 1. Login as the default user to get a JWT.
    const loginBody = new URLSearchParams();
    loginBody.set("username", email);
    loginBody.set("password", password);

    const loginResp = await fetch(`${base}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: loginBody.toString(),
    });
    if (!loginResp.ok) {
      const body = await loginResp.text().catch(() => "");
      hookLog("mint_key_login_failed", { status: loginResp.status, body: body.slice(0, 200) });
      return "";
    }

    const loginData = (await loginResp.json()) as Record<string, unknown>;
    const jwt = String(loginData.access_token ?? "");
    if (!jwt) {
      hookLog("mint_key_no_token");
      return "";
    }

    // 2. Try to reuse an existing API key first.
    try {
      const listResp = await fetch(`${base}/api/v1/auth/api-keys`, {
        headers: { Cookie: `auth_token=${jwt}` },
      });
      if (listResp.ok) {
        const keys = (await listResp.json()) as Array<Record<string, unknown>>;
        if (Array.isArray(keys) && keys.length > 0) {
          const existingKey = String(keys[0]?.key ?? keys[0]?.api_key ?? "");
          if (existingKey) {
            cacheApiKey(existingKey, base);
            return existingKey;
          }
        }
      }
    } catch {
      // Fall through to creating a new key.
    }

    // 3. Mint a new API key.
    const createResp = await fetch(`${base}/api/v1/auth/api-keys`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Cookie: `auth_token=${jwt}`,
      },
      body: JSON.stringify({ name: "vellum-owner-bootstrap" }),
    });
    if (!createResp.ok) {
      const body = await createResp.text().catch(() => "");
      hookLog("mint_key_create_failed", { status: createResp.status, body: body.slice(0, 200) });
      return "";
    }

    const payload = (await createResp.json()) as Record<string, unknown>;
    const key = String(payload.key ?? payload.api_key ?? "");
    if (key) {
      cacheApiKey(key, base);
    }
    return key;
  } catch (err) {
    hookLog("mint_key_error", { error: String(err).slice(0, 200) });
    return "";
  }
}

// ─── Session start guidance ───────────────────────────────────────────────────

/**
 * Build the system message shown to the model at session start.
 * Mirrors `_session_start_guidance` in the Python script.
 */
function sessionStartGuidance(
  mode: string,
  dataset: string,
  sessionId: string,
  ready: boolean,
): string {
  if (ready) {
    return (
      "## Cognee Memory Connected\n" +
      `Mode: ${mode} | Dataset: ${dataset} | Session: ${sessionId}\n\n` +
      "Cognee organizes knowledge into three categories.\n" +
      "- user_context: user preferences and personal facts\n" +
      "- project_docs: repository and project knowledge\n" +
      "- agent_actions: tool traces and agent findings\n\n" +
      "Use the cognee_recall tool or cognee-search skill to query memory, " +
      "and the cognee-remember skill to store permanent memory."
    );
  }
  return (
    "## Cognee Memory Connecting\n" +
    `Mode: ${mode} | Dataset: ${dataset} | Session: ${sessionId}\n\n` +
    "The local Cognee server is starting up (first run or database " +
    "migrations can take a little while). Your prompts work normally now; " +
    "memory recall activates automatically once the server is ready."
  );
}

// ─── Main entry point ─────────────────────────────────────────────────────────

/**
 * Initialize a Cognee memory session.
 *
 * @param conversationId The Vellum conversation ID (host session key).
 * @param cwd            The current working directory (for context).
 * @returns Session start result with system message + additional context.
 */
export async function startSession(
  conversationId: string,
  cwd = process.cwd(),
): Promise<SessionStartResult> {
  const cfg = loadConfig();
  const baseUrl = cfg.baseUrl.replace(/\/+$/, "");
  const dataset = cfg.dataset;

  // Set the session key env so downstream hooks/scripts can find it.
  const sessionKey = sanitizeSessionKey(conversationId);
  if (sessionKey) {
    process.env.COGNEE_SESSION_KEY = sessionKey;
  }

  if (!sessionKey) {
    hookLog("missing_session_key", { cwd });
    return {
      systemMessage: "Cognee Memory: session key missing in SessionStart payload.",
      additionalContext: "",
      ready: false,
      sessionId: "",
      dataset,
    };
  }

  // Compute the Cognee session ID from the host session key.
  const sessionId = resolveSessionId(sessionKey, cfg.agentName);
  if (sessionId) {
    process.env.COGNEE_SESSION_ID = sessionId;
  }
  hookLog("session_resolved", { sessionKey, sessionId });

  // Check if the backend is reachable.
  const reachable = await backendReachable(baseUrl);
  hookLog("endpoint_mode_selected", { baseUrl, server_live: reachable });

  // Resolve or mint the API key.
  // Priority: env var → cached key → mint from local server.
  let apiKey = resolveApiKey(baseUrl);
  if (!apiKey && reachable && isLocalUrl(baseUrl)) {
    const email = process.env.COGNEE_USER_EMAIL ?? "";
    const password = process.env.COGNEE_USER_PASSWORD ?? "";
    apiKey = await mintApiKey(baseUrl, email, password);
  }

  if (apiKey) {
    process.env.COGNEE_API_KEY = apiKey;
  } else {
    hookLog("no_api_key_resolved", { baseUrl, reachable });
  }

  let ready = false;

  if (reachable && apiKey) {
    // Mark the server as ready for hot-path recall.
    markServerReady();
    ready = true;

    // Ensure the dataset exists.
    try {
      await ensureDataset(baseUrl, apiKey, dataset);
    } catch (err) {
      hookLog("dataset_ensure_warning", { error: String(err).slice(0, 200) });
    }

    // Register the session as an active agent connection.
    // First check if already registered (idempotent).
    try {
      const existing = await resolveAgentConnection(baseUrl, apiKey, sessionId);
      if (!existing?.registered) {
        const registered = await registerAgent(baseUrl, apiKey, sessionId, [dataset]);
        hookLog("agent_register_result", {
          sessionId,
          registered,
          dataset,
        });
      } else {
        hookLog("agent_already_registered", { sessionId });
      }
    } catch (err) {
      hookLog("agent_register_error", { error: String(err).slice(0, 200) });
    }
  }

  // Create a config file on first run if it doesn't exist.
  const configPath = join(pluginStateDir(), "config.json");
  if (!existsSync(configPath)) {
    try {
      saveConfig(cfg);
    } catch {
      // Best-effort.
    }
  }

  // Reset the idle clock for this process before watchers start.
  touchActivity();

  const mode = isLocalUrl(baseUrl) ? "local" : "cloud";
  const systemMessage = sessionStartGuidance(mode, dataset, sessionId, ready);
  const additionalContext = memoryPreferenceSteer();

  hookLog("session_start_complete", {
    mode,
    sessionId,
    dataset,
    ready,
    hasKey: Boolean(apiKey),
  });

  return {
    systemMessage,
    additionalContext,
    ready,
    sessionId,
    dataset,
  };
}

/**
 * Convenience: run the full session-start sequence and return a JSON-serializable
 * hook output object (matching the Python script's stdout format).
 */
export async function runSessionStart(
  conversationId: string,
  cwd?: string,
): Promise<Record<string, unknown>> {
  try {
    const result = await startSession(conversationId, cwd);
    return {
      hookSpecificOutput: {
        hookEventName: "SessionStart",
        systemMessage: result.systemMessage,
        additionalContext: result.additionalContext,
      },
    };
  } catch (err) {
    hookLog("session_start_exception", { error: String(err).slice(0, 200) });
    return {};
  }
}

export default startSession;
