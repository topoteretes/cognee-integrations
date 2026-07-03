// ---------------------------------------------------------------------------
// Plugin version + "update available" check.
//
// Ported from the claude-code integration's version_check pattern:
//   - installed version comes from the plugin manifest (here: api.version, which
//     OpenClaw populates from package.json; PLUGIN_VERSION is the fallback);
//   - the latest published version comes from the plugin's distribution channel
//     (here: the npm registry, since OpenClaw installs via
//     `openclaw plugins install @cognee/cognee-openclaw`);
//   - the network check is TTL-gated and fail-silent, writing a small cache file
//     so the status surface reads it locally without ever hitting the network;
//   - a transient network error preserves the last known `latest` (never a false
//     "up to date", never a thrown error in a status command).
//
// Env knobs (shared with the claude-code integration for uniform behaviour):
//   COGNEE_UPDATE_CHECK=false             disable the check entirely
//   COGNEE_UPDATE_CHECK_INTERVAL_HOURS    re-check interval (default 24)
// ---------------------------------------------------------------------------

import { promises as fs } from "node:fs";
import { dirname } from "node:path";
import { UPDATE_CHECK_PATH } from "./persistence.js";

/**
 * Fallback version, surfaced only when `api.version` is undefined (some load
 * paths leave it unset). `__tests__/test_version.ts` asserts this equals
 * package.json's version so the two can never drift apart.
 */
export const PLUGIN_VERSION = "2026.6.11";

/** npm registry endpoint returning the latest published `{ version }`. */
export const NPM_LATEST_URL = "https://registry.npmjs.org/@cognee/cognee-openclaw/latest";

const DEFAULT_TTL_HOURS = 24;
const DEFAULT_TIMEOUT_MS = 2500;

export interface UpdateCheckRecord {
  /** Epoch millis of the last completed check. */
  checkedAt: number;
  installed: string;
  latest: string;
  updateAvailable: boolean;
}

// ---------------------------------------------------------------------------
// Version parsing / comparison
// ---------------------------------------------------------------------------

/**
 * Parse a dotted version into comparable integer segments. Tolerates a leading
 * `v` and a pre-release/build suffix (e.g. `v1.2.3-rc1` -> [1, 2, 3]) by
 * stopping each segment at the first non-digit.
 */
export function parseVersion(version: string): number[] {
  const parts: number[] = [];
  for (const chunk of String(version).trim().replace(/^[vV]/, "").split(".")) {
    let digits = "";
    for (const ch of chunk) {
      if (ch >= "0" && ch <= "9") digits += ch;
      else break;
    }
    parts.push(digits ? parseInt(digits, 10) : 0);
  }
  return parts;
}

/** Compare two versions numerically (so 4.2 < 4.12). Returns -1, 0, or 1. */
export function compareVersions(a: string, b: string): -1 | 0 | 1 {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x > y) return 1;
    if (x < y) return -1;
  }
  return 0;
}

/** True only when both versions are known and `latest` is strictly newer. */
export function isNewer(latest: string, installed: string): boolean {
  if (!latest || !installed) return false;
  return compareVersions(latest, installed) > 0;
}

/** A compact one-line badge pointing at the npm update command. */
export function formatUpdateBadge(latest: string): string {
  return `⬆ v${latest} available — openclaw plugins install @cognee/cognee-openclaw@latest`;
}

// ---------------------------------------------------------------------------
// Env knobs
// ---------------------------------------------------------------------------

/** Whether the update check is enabled (COGNEE_UPDATE_CHECK not falsey). */
export function updateCheckEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  const value = (env.COGNEE_UPDATE_CHECK ?? "").trim().toLowerCase();
  return !["0", "false", "no", "off"].includes(value);
}

/** Re-check interval in hours (COGNEE_UPDATE_CHECK_INTERVAL_HOURS, default 24). */
export function ttlHours(env: NodeJS.ProcessEnv = process.env): number {
  const raw = (env.COGNEE_UPDATE_CHECK_INTERVAL_HOURS ?? "").trim();
  const parsed = raw ? Number(raw) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_TTL_HOURS;
}

// ---------------------------------------------------------------------------
// Cache read + check runner
// ---------------------------------------------------------------------------

/** Read the cached check result. Pure-local, returns null on any failure. */
export async function readUpdateCache(
  statePath: string = UPDATE_CHECK_PATH,
): Promise<UpdateCheckRecord | null> {
  try {
    const raw = await fs.readFile(statePath, "utf-8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    return parsed as UpdateCheckRecord;
  } catch {
    return null;
  }
}

/** Fetch the latest published version from npm. Throws on network/non-2xx. */
async function fetchLatestVersion(
  url: string,
  timeoutMs: number,
  fetchImpl: typeof fetch,
): Promise<string> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchImpl(url, {
      signal: controller.signal,
      headers: { Accept: "application/json" },
    });
    if (!res.ok) throw new Error(`registry responded ${res.status}`);
    const body = (await res.json()) as { version?: unknown };
    return typeof body.version === "string" ? body.version.trim() : "";
  } finally {
    clearTimeout(timer);
  }
}

export interface RunUpdateCheckOptions {
  /** Installed version to compare against (api.version ?? PLUGIN_VERSION). */
  installed: string;
  statePath?: string;
  timeoutMs?: number;
  /** Bypass the TTL gate and check now. */
  force?: boolean;
  env?: NodeJS.ProcessEnv;
  /** Injectable clock (millis) for testing. */
  now?: () => number;
  /** Injectable fetch for testing. */
  fetchImpl?: typeof fetch;
  registryUrl?: string;
}

/**
 * Run the (TTL-gated, fail-silent) update check and refresh the cache.
 *
 * Returns null only when the check is disabled. On a recent check it returns the
 * cached record without any network call. On a network error it preserves the
 * previously-known `latest` so a transient outage never clears a real
 * notification.
 */
export async function runUpdateCheck(
  opts: RunUpdateCheckOptions,
): Promise<UpdateCheckRecord | null> {
  const {
    installed,
    statePath = UPDATE_CHECK_PATH,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    force = false,
    env = process.env,
    now = Date.now,
    fetchImpl = fetch,
    registryUrl = NPM_LATEST_URL,
  } = opts;

  if (!updateCheckEnabled(env)) return null;

  const prev = await readUpdateCache(statePath);

  // TTL gate: a recent check is a no-op — no network.
  if (!force && prev && now() - (prev.checkedAt ?? 0) < ttlHours(env) * 3600_000) {
    return prev;
  }

  let latest: string;
  try {
    latest = await fetchLatestVersion(registryUrl, timeoutMs, fetchImpl);
  } catch {
    // Network/parse failure: keep the last known `latest` so a transient outage
    // doesn't clear a real notification (and we still refresh checkedAt below,
    // so we don't hammer the registry on every trigger).
    latest = prev?.latest ?? "";
  }

  const record: UpdateCheckRecord = {
    checkedAt: now(),
    installed,
    latest,
    updateAvailable: isNewer(latest, installed),
  };

  try {
    await fs.mkdir(dirname(statePath), { recursive: true });
    await fs.writeFile(statePath, JSON.stringify(record), "utf-8");
  } catch {
    // best-effort cache write; a failure here must not break the caller
  }

  return record;
}
