/**
 * post-compact hook — fires after the loop compacts a conversation
 * mid-turn, before the turn resumes.
 *
 * Pulls a compact summary from the session cache (recent QAs, trace
 * feedback, graph context) and injects it as a system message at the
 * start of the compacted history so the model retains memory context
 * across compaction.
 */

import type { PostCompactContext, Message } from "@vellumai/plugin-api";

import { hookLog } from "../src/plugin-common.ts";
import { setSessionEnv } from "../src/bridge.ts";
import { postCompact } from "../src/post-compact.ts";

export default async function postCompactHook(
  ctx: PostCompactContext,
): Promise<Partial<PostCompactContext> | void> {
  const conversationId = ctx.conversationId;
  setSessionEnv(conversationId);

  try {
    const anchor = await postCompact(conversationId);
    if (anchor) {
      // The Message contract only allows user/assistant roles with
      // ContentBlock[] content, so re-inject the memory anchor as a
      // user-role text block rather than a (disallowed) system message.
      const anchorMessage: Message = {
        role: "user",
        content: [{ type: "text", text: anchor }],
      };
      ctx.history.unshift(anchorMessage);
    }
  } catch (err) {
    hookLog("post_compact_failed", { error: String(err).slice(0, 200) });
  }
}
