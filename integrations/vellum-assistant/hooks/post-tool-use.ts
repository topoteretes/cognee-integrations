/**
 * post-tool-use hook — fires after each tool returns, before the result
 * rejoins the history sent to the provider.
 *
 * Stores a TraceEntry to the Cognee session cache. Fire-and-forget,
 * never blocks the agent loop.
 */

import type { PostToolUseContext, Message } from "@vellumai/plugin-api";

import { hookLog, touchActivity } from "../src/plugin-common.ts";
import { setSessionEnv } from "../src/bridge.ts";
import { storeToolCall, type StoreToolPayload } from "../src/store-to-session.ts";

/**
 * Find the tool name and input for a tool_result by scanning the conversation
 * messages for the matching tool_use block (matched by tool_use_id).
 *
 * ToolResultContent only carries { type, tool_use_id, content, is_error } —
 * the tool name and input live on the corresponding ToolUseContent block
 * in the message history.
 */
function resolveToolMeta(
  messages: ReadonlyArray<Message>,
  toolUseId: string,
): { name: string; input: Record<string, unknown> | undefined } {
  for (const msg of messages) {
    for (const block of msg.content) {
      if (block.type === "tool_use" && block.id === toolUseId) {
        return { name: block.name, input: block.input };
      }
    }
  }
  return { name: "unknown", input: undefined };
}

export default async function postToolUse(
  ctx: PostToolUseContext,
): Promise<void> {
  const conversationId = ctx.conversationId;
  setSessionEnv(conversationId);

  // Extract tool result fields from the tool_response block.
  const toolResponse = ctx.toolResponse;
  const toolUseId = toolResponse.tool_use_id ?? "";
  const toolOutput = toolResponse.content ?? "";

  // The tool name and input are not on the tool_result — look them up
  // from the conversation history by matching tool_use_id.
  const { name: toolName, input: toolInput } = resolveToolMeta(
    ctx.messages,
    toolUseId,
  );

  touchActivity();

  const payload: StoreToolPayload = {
    tool_name: toolName,
    tool_input: toolInput,
    tool_output: toolOutput,
    tool_response: toolResponse,
    conversationId,
  };

  try {
    await storeToolCall(payload);
  } catch (err) {
    hookLog("post_tool_use_store_failed", {
      tool: toolName,
      error: String(err).slice(0, 200),
    });
  }
}
