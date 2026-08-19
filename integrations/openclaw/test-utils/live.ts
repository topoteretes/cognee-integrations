/**
 * Live-tier helpers: a real Cognee server, real LLM calls, a real graph.
 *
 * Everything else in this suite is hermetic — `MockCognee` proves the plugin sends
 * the right requests, but a mock has no graph, so nothing about *memory actually
 * working* is testable there. This tier closes that gap the same way the Python
 * integrations' `e2e/live` does.
 *
 * ## Opt-in, and never by default
 *
 * Requires BOTH `COGNEE_RUN_LIVE=1` and an explicit `COGNEE_LIVE_BASE_URL`. There
 * is deliberately **no default URL**: a developer running the suite normally has a
 * real Cognee on the conventional port holding real data, and a test tier that
 * defaulted to it would write into that graph. Naming the server is the consent.
 *
 * ## Why no server boot
 *
 * `gateway_start` skips its boot path entirely when `client.health()` already
 * answers, so pointing at a running server means the plugin connects rather than
 * writing `ensure_and_boot.py` and building a venv. That keeps this tier fast, and
 * the boot/install path is already covered by the Python live tier which shares the
 * same `~/.cognee-plugin` directory.
 *
 * ## Isolation
 *
 * Every run invents a `live_<uuid>` dataset, so runs never read each other's
 * memory, and `deleteTestDatasets` removes only that namespace — never
 * delete-everything, because the server may hold real data. The suite-wide
 * `os.homedir()` sandbox from `jest.setup.ts` still applies, so the plugin's local
 * state lands in a throwaway directory rather than the developer's home.
 */

import { randomUUID } from "node:crypto";

/** Dataset-name prefix owned by this tier; cleanup is scoped to it. */
export const TEST_DATASET_PREFIX = "live_";

export interface LiveConfig {
  baseUrl: string;
  apiKey: string;
  dataset: string;
  mode: "local" | "cloud";
}

/** True when the tier has been explicitly opted into with a named server. */
export function liveEnabled(): boolean {
  const optedIn = ["1", "true", "yes"].includes(
    (process.env.COGNEE_RUN_LIVE ?? "").trim().toLowerCase(),
  );
  return optedIn && Boolean(liveBaseUrl());
}

export function liveBaseUrl(): string {
  return (process.env.COGNEE_LIVE_BASE_URL ?? "").trim().replace(/\/+$/, "");
}

export function liveApiKey(): string {
  return (process.env.COGNEE_LIVE_API_KEY ?? "").trim();
}

/**
 * Why the tier is skipped, for a message that tells the reader what to set.
 * A bare "skipped" leaves someone guessing which of two variables is missing.
 */
export function liveSkipReason(): string {
  if (!["1", "true", "yes"].includes((process.env.COGNEE_RUN_LIVE ?? "").trim().toLowerCase())) {
    return "live tier is opt-in: set COGNEE_RUN_LIVE=1";
  }
  if (!liveBaseUrl()) {
    return "live tier needs COGNEE_LIVE_BASE_URL (no default — it would target a real server)";
  }
  return "";
}

/** Config for one run, with a dataset unique to it. */
export function liveConfig(): LiveConfig {
  const baseUrl = liveBaseUrl();
  return {
    baseUrl,
    apiKey: liveApiKey(),
    dataset: `${TEST_DATASET_PREFIX}${randomUUID().replace(/-/g, "").slice(0, 12)}`,
    // Loopback is a server the developer runs; anything else is treated as cloud,
    // matching the plugin's own `isLocalUrl` split.
    mode: /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:|$)/.test(baseUrl) ? "local" : "cloud",
  };
}

/**
 * A token that cannot exist in any model's training data.
 *
 * Every memory assertion hangs off one of these: asked about a nonce, a system
 * with no memory cannot produce the right answer by confabulating, so a pass is
 * evidence rather than coincidence.
 */
export function nonce(): string {
  return `ZEPHYR-${randomUUID().replace(/-/g, "").slice(0, 6).toUpperCase()}`;
}

async function api(
  cfg: LiveConfig,
  path: string,
  init: RequestInit = {},
): Promise<{ status: number; text: string }> {
  const prefix = cfg.mode === "cloud" ? "" : "/api/v1";
  const res = await fetch(`${cfg.baseUrl}${prefix}${path}`, {
    ...init,
    headers: { "X-Api-Key": cfg.apiKey, ...(init.headers as Record<string, string>) },
  });
  return { status: res.status, text: await res.text() };
}

/** `/health` status, or null when nothing answers. */
export async function serverHealth(cfg: LiveConfig): Promise<number | null> {
  try {
    const res = await fetch(`${cfg.baseUrl}/health`, { headers: { "X-Api-Key": cfg.apiKey } });
    return res.status;
  } catch {
    return null;
  }
}

/**
 * Query the graph directly, the way `cognee-search` would.
 *
 * Independent of the plugin on purpose: it separates "the write reached the graph"
 * from "the plugin can read it back", so a failure says which half broke.
 */
export async function recallFromGraph(cfg: LiveConfig, query: string, topK = 5): Promise<string> {
  const { status, text } = await api(cfg, "/recall", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, top_k: topK, only_context: true, scope: "auto", datasets: [cfg.dataset] }),
  });
  // A rejected request is not "the graph has not caught up yet". Distinguishing
  // them matters: the first version of this helper returned the body regardless,
  // so an unauthorized recall looked identical to an empty one and
  // `waitUntilRecalled` polled a 401 sixty times before timing out — ten minutes
  // spent to report the wrong cause.
  if (status < 200 || status >= 300) {
    throw new RecallRejected(status, text);
  }
  return text;
}

/** A recall the server refused, as opposed to one that simply found nothing. */
export class RecallRejected extends Error {
  constructor(
    readonly status: number,
    readonly body: string,
  ) {
    super(`recall rejected with HTTP ${status}: ${body.slice(0, 200)}`);
  }
}

/**
 * Poll the graph until every term appears. Returns the matching body.
 *
 * This is the readiness gate for a graph write, and it has to be polling: a
 * cognify is asynchronous, so any single read straight after a write is a race. An
 * "improve fired" style acknowledgement is not sufficient either — it can report
 * success while the graph is still building.
 */
export async function waitUntilRecalled(
  cfg: LiveConfig,
  query: string,
  terms: string[],
  { deadlineMs = 600_000, intervalMs = 10_000 }: { deadlineMs?: number; intervalMs?: number } = {},
): Promise<string> {
  const end = Date.now() + deadlineMs;
  let last = "";
  while (Date.now() < end) {
    try {
      last = await recallFromGraph(cfg, query);
    } catch (error) {
      // Auth and malformed-request failures will never resolve by waiting, so
      // surface them immediately rather than burning the whole deadline.
      //
      // 404 is deliberately NOT in that set. The obvious reading — "the endpoint
      // is wrong" — is not what it means here: the server answers
      // `DatasetNotFoundError` until the first write creates the dataset, which is
      // exactly the condition polling exists to wait out. Treating it as terminal
      // made the flagship fail instantly on a perfectly healthy run.
      if (error instanceof RecallRejected && [400, 401, 403, 422].includes(error.status)) {
        throw new Error(
          `recall cannot succeed as configured — ${error.message}\n` +
            `check COGNEE_LIVE_API_KEY: polling will not fix a rejected request.`,
        );
      }
      last = `<recall error: ${String(error)}>`;
    }
    const lowered = last.toLowerCase();
    if (terms.every((t) => lowered.includes(t.toLowerCase()))) return last;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(
    `graph never returned ${JSON.stringify(terms)} for ${JSON.stringify(query)} within ${deadlineMs}ms.\nlast body: ${last.slice(0, 1200)}`,
  );
}

/** `[id, name]` for datasets in this tier's namespace only. */
export async function listTestDatasets(cfg: LiveConfig): Promise<[string, string][]> {
  const { status, text } = await api(cfg, "/datasets");
  if (status < 200 || status >= 300) return [];
  try {
    const rows = JSON.parse(text) as { id?: string; name?: string }[];
    return (rows ?? [])
      .filter((r) => String(r?.name ?? "").startsWith(TEST_DATASET_PREFIX))
      .map((r) => [String(r.id ?? ""), String(r.name ?? "")] as [string, string]);
  } catch {
    return [];
  }
}

/**
 * Delete only this tier's datasets. Returns `[deleted, failures]`.
 *
 * Scoped by name, never `DELETE /datasets` (delete-all) and never
 * `forget {everything: true}`: the target server may hold real data, and cleanup
 * that is broader than what it cleans up is far worse than the residue it prevents.
 */
export async function deleteTestDatasets(cfg: LiveConfig): Promise<[number, string[]]> {
  const failures: string[] = [];
  let deleted = 0;
  for (const [id, name] of await listTestDatasets(cfg)) {
    if (!id) continue;
    const { status } = await api(cfg, `/datasets/${id}`, { method: "DELETE" });
    if (status >= 200 && status < 300) deleted += 1;
    else failures.push(`${name} (${status})`);
  }
  return [deleted, failures];
}
