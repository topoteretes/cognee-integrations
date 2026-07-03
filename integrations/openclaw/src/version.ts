// Plugin version reporting and a background npm update check.
//
// The installed version comes from api.version, which OpenClaw fills in from
// package.json when it loads the plugin. PLUGIN_VERSION is only a fallback for
// the load paths that leave api.version unset.
//
// The update check queries the npm registry, since the plugin installs with
// `openclaw plugins install @cognee/cognee-openclaw`. It is rate-limited and
// writes its result to a cache file, so the status command reads that result
// locally and never blocks on the network. A failed check keeps the last known
// result rather than clearing it.
//
// Environment variables:
//   COGNEE_UPDATE_CHECK=false            turn the check off
//   COGNEE_UPDATE_CHECK_INTERVAL_HOURS   hours between checks (default 24)

import { promises as fs } from "node:fs";
import { dirname } from "node:path";
import { UPDATE_CHECK_PATH } from "./persistence.js";

/**
 * Fallback version used when api.version is unset. The test in
 * __tests__/test_version.ts asserts this stays equal to package.json.
 */
export const PLUGIN_VERSION = "2026.6.11";

const NPM_LATEST_URL = "https://registry.npmjs.org/@cognee/cognee-openclaw/latest";
const DEFAULT_TTL_HOURS = 24;
const DEFAULT_TIMEOUT_MS = 2500;

export interface UpdateCheckRecord {
  checkedAt: number;
  installed: string;
  latest: string;
  updateAvailable: boolean;
}

/**
 * Split a version into integer parts for comparison. A leading "v" and any
 * pre-release suffix are ignored, so "v1.2.3-rc1" becomes [1, 2, 3].
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

/** Compare two versions numerically, so that 4.2 sorts below 4.12. */
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

/** True only when both versions are known and latest is strictly newer. */
export function isNewer(latest: string, installed: string): boolean {
  if (!latest || !installed) return false;
  return compareVersions(latest, installed) > 0;
}

/** One-line notice shown when a newer version is available. */
export function formatUpdateHint(latest: string): string {
  return `Update available: v${latest}. Run: openclaw plugins install @cognee/cognee-openclaw@latest`;
}

function updateCheckEnabled(env: NodeJS.ProcessEnv): boolean {
  const value = (env.COGNEE_UPDATE_CHECK ?? "").trim().toLowerCase();
  return !["0", "false", "no", "off"].includes(value);
}

function ttlHours(env: NodeJS.ProcessEnv): number {
  const raw = (env.COGNEE_UPDATE_CHECK_INTERVAL_HOURS ?? "").trim();
  const parsed = raw ? Number(raw) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_TTL_HOURS;
}

/** Read the cached check result. Returns null when the file is missing or invalid. */
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

/** Fetch the latest published version from npm. Throws on a network or non-2xx error. */
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
  /** Skip the interval gate and check now. */
  force?: boolean;
  env?: NodeJS.ProcessEnv;
  /** Clock override for tests. */
  now?: () => number;
  /** fetch override for tests. */
  fetchImpl?: typeof fetch;
  registryUrl?: string;
}

/**
 * Run the update check and refresh the cache.
 *
 * Returns null when the check is disabled. Within the interval it returns the
 * cached record without any network call. If the fetch fails it keeps the last
 * known latest version so a temporary outage does not clear a real notice.
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
  if (!force && prev && now() - (prev.checkedAt ?? 0) < ttlHours(env) * 3_600_000) {
    return prev;
  }

  let latest: string;
  try {
    latest = await fetchLatestVersion(registryUrl, timeoutMs, fetchImpl);
  } catch {
    // Keep the last known latest version and refresh the timestamp, so a
    // temporary outage neither clears the notice nor triggers repeated retries.
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
    // The cache write is best effort and must not fail the caller.
  }

  return record;
}
