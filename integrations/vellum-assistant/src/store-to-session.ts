/**
 * Store tool calls and assistant responses into the Cognee session cache.
 *
 * Ported from store-to-session.py. Two modes:
 *
 *   Normal (tool call): Write a TraceEntry via storeEntry() with
 *     origin_function, method_params, method_return_value, status.
 *
 *   Stop (--stop flag): Read the pending prompt, pair it with the
 *     assistant's final message, store a QAEntry via storeEntry().
 *
 * Both modes also record in the bridge cache (recordBridgeQA /
 * recordBridgeTrace) for later graph sync.
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
  isServerReady,
  touchActivity,
  bumpSaveCounter,
  stagePendingPrompt,
  consumePendingPrompt,
  recordBridgeQA,
  recordBridgeTrace,
  type PendingPrompt,
} from "./plugin-common.ts";
import { storeEntry, backendReachable } from "./cognee-client.ts";

// ─── Constants ────────────────────────────────────────────────────────────────

const MAX_PARAMS_BYTES = 4000;
const MAX_RETURN_BYTES = 8000;
const MAX_ASSISTANT_BYTES = 8000;

// ─── Helpers ──────────────────────────────────────────────────────────────────

function truncateStr(value: unknown, cap: number): string {
  if (value === null || value === undefined) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  if (text.length <= cap) return text;
  return text.slice(0, cap - 3) + "...";
}

interface InferredStatus {
  status: string;
  errorMessage: string;
}

function inferStatus(payload: StoreToolPayload): InferredStatus {
  const response = (payload.tool_response ?? payload.tool_output ?? "") as unknown;
  if (typeof response === "object" && response !== null) {
    const resp = response as Record<string, unknown>;
    if (resp.is_error || resp.error) {
      const err = String(resp.error ?? resp.message ?? "Tool reported an error.");
      return { status: "error", errorMessage: truncateStr(err, 500) };
    }
  }
  if (typeof payload.error === "string" && payload.error) {
    return { status: "error", errorMessage: truncateStr(payload.error, 500) };
  }
  return { status: "success", errorMessage: "" };
}

// ─── Types ────────────────────────────────────────────────────────────────────

export interface StoreToolPayload {
  tool_name: string;
  tool_input: unknown;
  tool_output: unknown;
  tool_response?: unknown;
  error?: string;
  conversationId?: string;
}

export interface StoreStopPayload {
  assistant_message: string;
  conversationId?: string;
  turn_id?: string;
}

// ─── Session resolution ───────────────────────────────────────────────────────

function loadSession(conversationId?: string): { sessionId: string; dataset: string } {
  const cfg = loadConfig();
  let sessionKey = getSessionKey();
  if (!sessionKey && conversationId) {
    sessionKey = sanitizeSessionKey(conversationId);
  }
  const sessionId = resolveSessionId(sessionKey, cfg.agentName);
  return { sessionId, dataset: cfg.dataset };
}

// ─── Tool call storage ────────────────────────────────────────────────────────

/**
 * Store a PostToolUse event as a TraceEntry.
 *
 * @param payload Tool call details (name, input, output, status).
 */
export async function storeToolCall(payload: StoreToolPayload): Promise<void> {
  const toolName = payload.tool_name || "unknown";
  const toolInput = payload.tool_input ?? {};
  const toolOutput = payload.tool_output ?? payload.tool_response ?? "";

  // Suppress self-reference: Bash calls mentioning 'cognee' are the plugin
  // talking to itself and would recurse.
  if (toolName === "Bash") {
    let cmd = "";
    if (typeof toolInput === "object" && toolInput !== null) {
      cmd = String((toolInput as Record<string, unknown>).command ?? "");
    }
    if (cmd.includes("cognee")) {
      hookLog("skip_self_cognee_bash", { cmd_prefix: cmd.slice(0, 80) });
      return;
    }
  }

  const { status, errorMessage } = inferStatus(payload);

  // Normalize method_params.
  let params: Record<string, string>;
  if (typeof toolInput === "object" && toolInput !== null && !Array.isArray(toolInput)) {
    params = {};
    for (const [k, v] of Object.entries(toolInput as Record<string, unknown>)) {
      params[k] = truncateStr(v, MAX_PARAMS_BYTES);
    }
  } else {
    params = { value: truncateStr(toolInput, MAX_PARAMS_BYTES) };
  }

  const returnValue = truncateStr(toolOutput, MAX_RETURN_BYTES);

  const { sessionId, dataset } = loadSession(payload.conversationId);
  if (!sessionId) {
    hookLog("no_session_id", { tool: toolName });
    return;
  }

  const { baseUrl, apiKey } = resolveHttpEndpoint();
  if (!apiKey) {
    hookLog("store_tool_no_api_key", { tool: toolName });
    return;
  }

  // Readiness gate: if the server is still warming, buffer into the bridge
  // shadow instead of dropping the trace.
  if (!isServerReady()) {
    if (!(await backendReachable(baseUrl, 1500))) {
      const traceText =
        `${toolName} [${status}]\n` +
        `Params: ${JSON.stringify(params)}\n` +
        `Return: ${returnValue}`;
      recordBridgeTrace(sessionId, dataset, traceText);
      bumpSaveCounter("trace");
      hookLog("store_buffered_warming", { hook: "tool", tool: toolName });
      return;
    }
  }

  const entry: Record<string, unknown> = {
    type: "trace",
    origin_function: toolName,
    status,
    method_params: params,
    method_return_value: returnValue,
    error_message: errorMessage,
    generate_feedback_with_llm: false,
  };

  try {
    const ok = await storeEntry(baseUrl, apiKey, entry, dataset, sessionId);
    if (ok) {
      const traceText =
        `${toolName} [${status}]\n` +
        `Params: ${JSON.stringify(params)}\n` +
        `Return: ${returnValue}`;
      recordBridgeTrace(sessionId, dataset, traceText);
      bumpSaveCounter("trace");
      touchActivity();
      hookLog("trace_stored", { tool: toolName, status });
    } else {
      hookLog("trace_store_noresult", { tool: toolName });
    }
  } catch (err) {
    hookLog("trace_store_error", { tool: toolName, error: String(err).slice(0, 200) });
  }
}

// ─── Stop / assistant message storage ─────────────────────────────────────────

/**
 * Store a Stop-hook payload (final assistant message) as a QAEntry.
 * Pairs the pending prompt (staged by store-user-prompt) with the answer.
 *
 * @param payload Stop details including the final assistant message.
 */
export async function storeAssistantStop(payload: StoreStopPayload): Promise<void> {
  const msg = payload.assistant_message;
  if (!msg || msg === "null") return;

  const truncatedMsg = truncateStr(msg, MAX_ASSISTANT_BYTES);

  const { sessionId, dataset } = loadSession(payload.conversationId);
  if (!sessionId) {
    hookLog("no_session_id", { event: "stop" });
    return;
  }

  const sessionKey = getSessionKey() || sanitizeSessionKey(payload.conversationId ?? "");
  const { baseUrl, apiKey } = resolveHttpEndpoint();
  if (!apiKey) {
    hookLog("store_stop_no_api_key");
    return;
  }

  // Readiness gate.
  if (!isServerReady()) {
    if (!(await backendReachable(baseUrl, 1500))) {
      const pending = consumePendingPrompt(sessionKey);
      recordBridgeQA(sessionId, dataset, pending?.prompt ?? "", truncatedMsg);
      bumpSaveCounter("answer");
      hookLog("store_buffered_warming", { hook: "stop" });
      return;
    }
  }

  // Consume the pending prompt that was staged by store-user-prompt.
  const pending: PendingPrompt | null = consumePendingPrompt(sessionKey);

  const entry: Record<string, unknown> = {
    type: "qa",
    question: pending?.prompt ?? "",
    answer: truncatedMsg,
    context: "",
  };

  try {
    const ok = await storeEntry(baseUrl, apiKey, entry, dataset, sessionId);
    if (ok) {
      recordBridgeQA(sessionId, dataset, pending?.prompt ?? "", truncatedMsg);
      bumpSaveCounter("answer");
      touchActivity();
      hookLog("stop_stored", { chars: truncatedMsg.length });
    }
  } catch (err) {
    hookLog("stop_store_error", { error: String(err).slice(0, 200) });
  }
}

export default { storeToolCall, storeAssistantStop };
