/**
 * Store the user's prompt until the Stop hook provides the assistant answer.
 *
 * Ported from store-user-prompt.py. Runs on the UserPromptSubmit hook so it
 * doesn't block the parallel context-lookup. The prompt is staged (pending)
 * and a single paired QAEntry is written on Stop.
 *
 * Best-effort: never throws from hooks.
 */

import {
  loadConfig,
  hookLog,
  getSessionKey,
  resolveSessionId,
  sanitizeSessionKey,
  resolveHttpEndpoint,
  touchActivity,
  stagePendingPrompt,
  bumpSaveCounter,
} from "./plugin-common.ts";

const MAX_TEXT = 4000;

/**
 * Stage the user's prompt for later pairing with the assistant's response.
 *
 * @param prompt         The user's prompt text.
 * @param conversationId The Vellum conversation ID (host session key).
 * @param cwd            The current working directory (stored with the prompt).
 */
export async function storeUserPrompt(
  prompt: string,
  conversationId: string,
  cwd = process.cwd(),
): Promise<void> {
  if (!prompt || prompt.length < 5) return;

  const sessionKey = sanitizeSessionKey(conversationId);
  if (!sessionKey) {
    hookLog("prompt_missing_session_key");
    return;
  }

  const cfg = loadConfig();
  const sessionId = resolveSessionId(sessionKey, cfg.agentName);
  if (!sessionId) {
    hookLog("no_session_id", { event: "prompt" });
    return;
  }

  // Touch the activity file so the idle watcher knows we're alive.
  touchActivity();

  // Stage the prompt — it will be consumed by store-to-session on Stop.
  stagePendingPrompt(sessionKey, prompt.slice(0, MAX_TEXT), cwd);

  bumpSaveCounter("prompt");
  hookLog("prompt_pending", { chars: prompt.length, sessionKey });

  // Also set the env so the stop hook can find it.
  process.env.COGNEE_SESSION_KEY = sessionKey;
}

export default storeUserPrompt;
