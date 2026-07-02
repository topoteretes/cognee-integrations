/**
 * stop hook — fires once per run, when the loop has committed to ending
 * the turn.
 *
 * Pairs the staged user prompt with the assistant's final response and
 * stores a QAEntry to the Cognee session cache. Also bumps the save
 * counter and triggers a graph sync if the threshold is reached.
 */

import type { StopContext, Message } from "@vellumai/plugin-api";

import {
  hookLog,
  touchActivity,
  bumpSaveCounter,
} from "../src/plugin-common.ts";
import { setSessionEnv } from "../src/bridge.ts";
import { storeAssistantStop, type StoreStopPayload } from "../src/store-to-session.ts";
import { syncSessionToGraph } from "../src/sync-session-to-graph.ts";

/** Extract the last assistant message's text from the conversation history. */
function lastAssistantText(messages: ReadonlyArray<Message>): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.role === "assistant") {
      if (typeof msg.content === "string") {
        return msg.content;
      }
      if (Array.isArray(msg.content)) {
        const texts = msg.content
          .filter((b: unknown) => {
            const block = b as Record<string, unknown>;
            return block?.type === "text" && typeof block.text === "string";
          })
          .map((b: unknown) => (b as Record<string, unknown>).text as string);
        if (texts.length > 0) {
          return texts.join("\n");
        }
      }
    }
  }
  return "";
}

export default async function stop(ctx: StopContext): Promise<void> {
  const conversationId = ctx.conversationId;
  setSessionEnv(conversationId);

  const assistantMessage = lastAssistantText(ctx.messages);
  if (!assistantMessage || assistantMessage.length < 5) return;

  touchActivity();

  const payload: StoreStopPayload = {
    assistant_message: assistantMessage,
    conversationId,
  };

  try {
    await storeAssistantStop(payload);

    // Bump save counter and check if we should auto-sync.
    const shouldSync = bumpSaveCounter("answer");
    if (shouldSync) {
      syncSessionToGraph(false).catch((err) => {
        hookLog("auto_sync_failed", { error: String(err).slice(0, 200) });
      });
    }
  } catch (err) {
    hookLog("stop_store_failed", { error: String(err).slice(0, 200) });
  }
}
