/**
 * Cognee HTTP client — the single transport layer for all Cognee API calls.
 *
 * Ported from _cognee_client.py + _recall_http.py + _remember_http.py.
 * Uses Bun's native fetch. Includes a file-based circuit breaker so a
 * repeatedly-failing backend trips one breaker instead of being hammered
 * on every call.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, dirname } from "node:path";

// ─── Types ───────────────────────────────────────────────────────────────────

export type RecallResult = unknown[] | Record<string, unknown> | typeof UNREACHABLE;

export const UNREACHABLE = "UNREACHABLE" as const;

export interface CogneeConfig {
  baseUrl: string;
  apiKey: string;
  dataset: string;
  sessionId: string;
}

// ─── Circuit breaker ──────────────────────────────────────────────────────────

const THRESHOLD = Number(process.env.COGNEE_BREAKER_THRESHOLD ?? 5);
const COOLDOWN = Number(process.env.COGNEE_BREAKER_COOLDOWN ?? 120);

function stateDir(): string {
  return process.env.COGNEE_PLUGIN_STATE_DIR ?? join(homedir(), ".cognee-plugin");
}

function breakerPath(): string {
  return join(stateDir(), "recall-breaker.json");
}

interface BreakerState {
  failures: number;
  cooldown_until: number;
  last_error?: string;
}

function readBreaker(): BreakerState {
  try {
    return JSON.parse(readFileSync(breakerPath(), "utf-8"));
  } catch {
    return { failures: 0, cooldown_until: 0 };
  }
}

function writeBreaker(state: BreakerState): void {
  try {
    const p = breakerPath();
    mkdirSync(dirname(p), { recursive: true });
    writeFileSync(p, JSON.stringify(state), "utf-8");
  } catch {
    // Best-effort.
  }
}

export function breakerOpen(): [boolean, number] {
  const now = Date.now() / 1000;
  const until = readBreaker().cooldown_until ?? 0;
  return now < until ? [true, Math.ceil(until - now)] : [false, 0];
}

export function recordFailure(error: string): void {
  const now = Date.now() / 1000;
  const state = readBreaker();
  state.failures = (state.failures ?? 0) + 1;
  state.last_error = error.slice(0, 200);
  if (state.failures >= THRESHOLD) {
    state.cooldown_until = now + COOLDOWN;
  }
  writeBreaker(state);
}

export function recordSuccess(): void {
  writeBreaker({ failures: 0, cooldown_until: 0 });
}

// ─── HTTP helpers ─────────────────────────────────────────────────────────────

function errorEnvelope(status: number, message: string): Record<string, unknown> {
  return { status, error: message };
}

/**
 * Recall against Cognee's /api/v1/recall.
 *
 * Returns:
 *   - A JSON array on 2xx (empty array = authoritative "no hits")
 *   - An error envelope dict on non-2xx
 *   - UNREACHABLE sentinel when the server can't be reached
 */
export async function doRecall(
  baseUrl: string,
  apiKey: string,
  query: string,
  sessionId: string,
  scope: string[],
  topK: number,
  dataset = "",
  contextProfile = "",
  timeoutMs = 20_000,
): Promise<RecallResult> {
  const url = `${baseUrl.replace(/\/+$/, "")}/api/v1/recall`;
  const body: Record<string, unknown> = {
    query,
    top_k: topK,
    only_context: true,
    scope,
  };
  if (sessionId) body.session_id = sessionId;
  if (dataset) body.datasets = [dataset];
  if (contextProfile) body.context_profile = contextProfile;

  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Api-Key": apiKey,
      },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    clearTimeout(timer);

    if (resp.status === 429) {
      return errorEnvelope(429, "rate limited");
    }

    const text = await resp.text();
    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch {
      return errorEnvelope(resp.status, `non-JSON response: ${text.slice(0, 200)}`);
    }

    if (resp.status === 200) {
      // The server returns either a list or { history: [...] }.
      if (Array.isArray(data)) return data;
      if (typeof data === "object" && data !== null && "history" in data) {
        const arr = (data as Record<string, unknown>).history;
        return Array.isArray(arr) ? arr : [data];
      }
      return [data];
    }

    const errMsg =
      typeof data === "object" && data !== null && "error" in data
        ? String((data as Record<string, unknown>).error).slice(0, 200)
        : `HTTP ${resp.status}`;
    return errorEnvelope(resp.status, errMsg);
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      return UNREACHABLE;
    }
    return UNREACHABLE;
  }
}

/**
 * Breaker-wrapped recall. Only genuine backend trouble (UNREACHABLE or 5xx)
 * trips the breaker; a reachable 4xx is a config problem, not a retry candidate.
 */
export async function recall(
  baseUrl: string,
  apiKey: string,
  query: string,
  sessionId: string,
  scope: string[],
  topK: number,
  dataset = "",
  timeoutMs?: number,
): Promise<RecallResult> {
  const [isOpen, retryIn] = breakerOpen();
  if (isOpen) {
    return errorEnvelope(
      503,
      `cognee temporarily unavailable (circuit open, retry in ~${retryIn}s)`,
    );
  }

  const result = await doRecall(
    baseUrl,
    apiKey,
    query,
    sessionId,
    scope,
    topK,
    dataset,
    "",
    timeoutMs ?? 20_000,
  );

  if (result === UNREACHABLE) {
    recordFailure("unreachable");
  } else if (
    typeof result === "object" &&
    result !== null &&
    !Array.isArray(result) &&
    Number((result as Record<string, unknown>).status ?? 0) >= 500
  ) {
    recordFailure(`http ${(result as Record<string, unknown>).status}`);
  } else {
    recordSuccess();
  }
  return result;
}

/**
 * Remember against Cognee's /api/v1/remember (multipart form).
 *
 * Returns:
 *   - { ok: true } on 2xx
 *   - An error envelope dict on non-2xx
 *   - UNREACHABLE sentinel when the server can't be reached
 */
export async function doRemember(
  baseUrl: string,
  apiKey: string,
  content: string,
  dataset: string,
  nodeSet: string,
  timeoutMs = 30_000,
): Promise<Record<string, unknown> | typeof UNREACHABLE> {
  const url = `${baseUrl.replace(/\/+$/, "")}/api/v1/remember`;
  const boundary = `----cognee-plugin${Date.now()}`;
  const fields: Record<string, string> = {
    datasetName: dataset,
    node_set: nodeSet,
    run_in_background: "false",
  };

  // Build multipart body manually (Bun doesn't have FormData multipart control).
  const parts: Uint8Array[] = [];
  const encoder = new TextEncoder();
  for (const [key, value] of Object.entries(fields)) {
    parts.push(
      encoder.encode(
        `--${boundary}\r\nContent-Disposition: form-data; name="${key}"\r\n\r\n${value}\r\n`,
      ),
    );
  }
  parts.push(
    encoder.encode(
      `--${boundary}\r\nContent-Disposition: form-data; name="data"; filename="${nodeSet}.txt"\r\n` +
        `Content-Type: text/plain\r\n\r\n`,
    ),
  );
  parts.push(encoder.encode(content));
  parts.push(encoder.encode(`\r\n--${boundary}--\r\n`));

  const body = new Blob(parts as BlobPart[]);

  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": `multipart/form-data; boundary=${boundary}`,
        "X-Api-Key": apiKey,
      },
      body,
      signal: ctrl.signal,
    });
    clearTimeout(timer);

    if (resp.status === 429) {
      return errorEnvelope(429, "rate limited");
    }

    const text = await resp.text();
    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch {
      data = {};
    }

    if (resp.status >= 200 && resp.status < 300) {
      return { ok: true };
    }

    const errMsg =
      typeof data === "object" && data !== null && "error" in data
        ? String((data as Record<string, unknown>).error).slice(0, 200)
        : `HTTP ${resp.status}`;
    return errorEnvelope(resp.status, errMsg);
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      return UNREACHABLE;
    }
    return UNREACHABLE;
  }
}

// ─── Agent connection API ────────────────────────────────────────────────────

/**
 * Resolve the agent connection for a given session name.
 * Returns { sessionId, datasets[], registered }.
 */
export async function resolveAgentConnection(
  baseUrl: string,
  apiKey: string,
  agentSessionName: string,
  timeoutMs = 5_000,
): Promise<{ sessionId: string; datasets: string[]; registered: boolean } | null> {
  const url = `${baseUrl.replace(/\/+$/, "")}/api/v1/agents/connections/me?agent_session_name=${encodeURIComponent(agentSessionName)}`;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const resp = await fetch(url, {
      headers: { "X-Api-Key": apiKey },
      signal: ctrl.signal,
    });
    clearTimeout(timer);

    if (resp.status !== 200) return null;
    const data = await resp.json() as Record<string, unknown>;
    const agent = (data.agent ?? {}) as Record<string, unknown>;
    const datasets = Array.isArray(agent.datasets)
      ? (agent.datasets as Array<Record<string, unknown>>)
          .map((d) => String(d?.name ?? ""))
          .filter(Boolean)
      : [];
    return {
      sessionId: String(agent.session_id ?? ""),
      datasets,
      registered: Boolean(agent.registered ?? false),
    };
  } catch {
    return null;
  }
}

/**
 * Register an agent connection.
 */
export async function registerAgent(
  baseUrl: string,
  apiKey: string,
  handle: string,
  datasets: string[],
  timeoutMs = 10_000,
): Promise<boolean> {
  const url = `${baseUrl.replace(/\/+$/, "")}/api/v1/agents/register`;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Api-Key": apiKey,
      },
      body: JSON.stringify({
        agent_session_name: handle,
        dataset_names: datasets,
        type: "api",
        memory_mode: "hybrid",
      }),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    return resp.status >= 200 && resp.status < 300;
  } catch {
    return false;
  }
}

/**
 * Unregister an agent connection.
 */
export async function unregisterAgent(
  baseUrl: string,
  apiKey: string,
  handle: string,
  timeoutMs = 5_000,
): Promise<boolean> {
  const url = `${baseUrl.replace(/\/+$/, "")}/api/v1/agents/unregister`;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Api-Key": apiKey,
      },
      body: JSON.stringify({ agent_session_name: handle }),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    return resp.status >= 200 && resp.status < 300;
  } catch {
    return false;
  }
}

/**
 * Ensure a dataset exists.
 */
export async function ensureDataset(
  baseUrl: string,
  apiKey: string,
  datasetName: string,
  timeoutMs = 10_000,
): Promise<boolean> {
  const url = `${baseUrl.replace(/\/+$/, "")}/api/v1/datasets`;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Api-Key": apiKey,
      },
      body: JSON.stringify({ name: datasetName }),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    return resp.status >= 200 && resp.status < 300;
  } catch {
    return false;
  }
}

/**
 * Check if the backend is reachable.
 */
export async function backendReachable(baseUrl: string, timeoutMs = 3_000): Promise<boolean> {
  const url = `${baseUrl.replace(/\/+$/, "")}/health`;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const resp = await fetch(url, { signal: ctrl.signal });
    clearTimeout(timer);
    return resp.status >= 200 && resp.status < 500;
  } catch {
    return false;
  }
}

/**
 * Check whether the Cognee server has an LLM API key configured.
 * The /api/v1/remember endpoint (graph sync) requires an LLM key for the
 * cognify pipeline. Without it, session-to-graph sync will fail with
 * LLMAPIKeyNotSetError. Session cache (remember/entry) works without it.
 *
 * Returns true if an LLM key is configured, false if not, null if unknown
 * (e.g. the settings endpoint is unreachable or requires auth we don't have).
 */
export async function checkLlmKey(
  baseUrl: string,
  apiKey: string,
  timeoutMs = 5_000,
): Promise<boolean | null> {
  const url = `${baseUrl.replace(/\/+$/, "")}/api/v1/settings`;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const resp = await fetch(url, {
      headers: { "X-Api-Key": apiKey },
      signal: ctrl.signal,
    });
    clearTimeout(timer);

    if (resp.status !== 200) return null;
    const data = await resp.json() as Record<string, unknown>;
    const llm = (data.llm ?? data.llm_provider ?? {}) as Record<string, unknown>;
    const key = String(llm.api_key ?? llm.apiKey ?? "");
    return Boolean(key);
  } catch {
    return null;
  }
}

/**
 * Store a QA or trace entry via /api/v1/remember/entry.
 */
export async function storeEntry(
  baseUrl: string,
  apiKey: string,
  entry: Record<string, unknown>,
  datasetName: string,
  sessionId: string,
  timeoutMs = 10_000,
): Promise<boolean> {
  const url = `${baseUrl.replace(/\/+$/, "")}/api/v1/remember/entry`;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Api-Key": apiKey,
      },
      body: JSON.stringify({
        entry,
        dataset_name: datasetName,
        session_id: sessionId,
      }),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    return resp.status >= 200 && resp.status < 300;
  } catch {
    return false;
  }
}
