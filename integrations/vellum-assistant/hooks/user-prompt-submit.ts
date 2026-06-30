/**
 * user-prompt-submit hook — fires once per user turn, after messages are
 * assembled and before the agent loop runs.
 *
 *   1. Resolves the Cognee session for this conversation
 *   2. Searches Cognee memory (session cache + permanent graph) for context
 *   3. Injects relevant context into latestMessages so the model sees it
 *   4. Stages the prompt for later pairing with the assistant's response on Stop
 *   5. Registers the agent connection if not already registered
 */

import type { UserPromptSubmitContext, Message } from "@vellumai/plugin-api";

import {
  loadConfig,
  resolveSessionId,
  sanitizeSessionKey,
  hookLog,
  touchActivity,
  resolveHttpEndpoint,
} from "../src/plugin-common.ts";
import {
  resolveAgentConnection,
  registerAgent,
} from "../src/cognee-client.ts";
import { setSessionEnv } from "../src/bridge.ts";
import { searchContext } from "../src/session-context-lookup.ts";
import { storeUserPrompt } from "../src/store-user-prompt.ts";

export default async function userPromptSubmit(
  ctx: UserPromptSubmitContext,
): Promise<Partial<UserPromptSubmitContext> | void> {
  const prompt = ctx.prompt;
  if (!prompt || prompt.length < 5) return;

  const conversationId = ctx.conversationId;
  setSessionEnv(conversationId);

  const cfg = loadConfig();
  const { baseUrl, apiKey } = resolveHttpEndpoint();

  if (!apiKey) {
    hookLog("user_prompt_skip_no_api_key");
    return;
  }

  const sessionKey = sanitizeSessionKey(conversationId);
  const sessionId = resolveSessionId(sessionKey, cfg.agentName);
  touchActivity();

  // 1. Register the agent connection if not already registered.
  try {
    const conn = await resolveAgentConnection(baseUrl, apiKey, sessionKey);
    if (!conn?.registered) {
      await registerAgent(baseUrl, apiKey, sessionKey, [cfg.dataset]);
    }
  } catch {
    // Non-fatal — proceed with context lookup.
  }

  // 2. Context lookup (synchronous — the model needs the results before answering).
  try {
    const context = await searchContext(prompt, sessionId, cfg.dataset);
    if (context) {
      // The Message contract only allows user/assistant roles with
      // ContentBlock[] content, so inject recalled memory as a labeled
      // user-role text block rather than a (disallowed) system message.
      const contextMessage: Message = {
        role: "user",
        content: [{ type: "text", text: context }],
      };
      ctx.latestMessages.push(contextMessage);
    }
  } catch (err) {
    hookLog("context_lookup_failed", { error: String(err).slice(0, 200) });
  }

  // 3. Stage the prompt for pairing with the assistant response on Stop.
  try {
    await storeUserPrompt(prompt, conversationId, process.cwd());
  } catch {
    // Fire-and-forget.
  }
}
