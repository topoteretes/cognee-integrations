/**
 * Runtime capability probing + cache for a cognee server: recall route
 * (`/recall` vs legacy `/search`), remember route (vs `/add`+`/cognify`),
 * remember/entry route, and auth prefix (`/api/v1/auth` vs `/auth`).
 *
 * No extra probe request is fired: `resolveRecallPath`/`resolveAuthPrefix`
 * drive the real request `client.ts` already needed, fall back once on a
 * 404, and cache the winner per base URL for the process lifetime.
 */

import { CogneeApiError } from "./errors.js";

// ---------------------------------------------------------------------------
// Capability shape + per-base-URL cache
// ---------------------------------------------------------------------------

export type RecallPath = "/api/v1/recall" | "/api/v1/search";
export type AuthPrefix = "/api/v1/auth" | "/auth";

/**
 * What has been learned about one cognee base URL so far. Every field starts
 * `undefined` ("unknown, not yet probed") and is set at most a handful of
 * times: `recallPath`/`authPrefix` are set the first time either call
 * resolves (success on the primary path also "sets" it, so the common case —
 * a fully up-to-date server — writes this exactly once per field with zero
 * extra network cost); `rememberSupported`/`rememberEntrySupported` are only
 * ever written to `false` (the "confirmed absent, degrade forever" case) —
 * a `true` value is never persisted for them because the happy path never
 * needs to remember success, only the one-time discovery of absence.
 */
export interface CogneeCapabilities {
  recallPath?: RecallPath;
  rememberSupported?: boolean;
  rememberEntrySupported?: boolean;
  authPrefix?: AuthPrefix;
}

/**
 * cognee's `get_remember_router.py` handler for `/remember` ingests the
 * data and runs cognify as one pipeline before responding — a fixed fact
 * about the endpoint's contract, not a runtime probe result. It only
 * matters to the `add()`+`cognify()` fallback leg: that leg must call
 * `cognify()` itself precisely because plain `add()` does not. Exported so
 * `client.ts`'s fallback path can reference why it calls cognify
 * explicitly, rather than hard-coding the reasoning inline.
 */
export const REMEMBER_IMPLIES_COGNIFY = true as const;

const registry = new Map<string, CogneeCapabilities>();

/**
 * One capability record per cognee base URL, shared by every `CogneeClient`
 * instance pointed at that URL, for the life of the process. Two clients
 * constructed against the same `baseUrl` — e.g. one used by the input
 * processor, one by a tool — share a single decision instead of each
 * paying their own 404.
 */
export function getCapabilities(baseUrl: string): CogneeCapabilities {
  let entry = registry.get(baseUrl);
  if (!entry) {
    entry = {};
    registry.set(baseUrl, entry);
  }
  return entry;
}

/**
 * Test-only escape hatch: drop cached capability state so a test can exercise
 * the probe path again against a fresh mock server. Not called anywhere in
 * production code.
 */
export function resetCapabilities(baseUrl?: string): void {
  if (baseUrl) registry.delete(baseUrl);
  else registry.clear();
}

// ---------------------------------------------------------------------------
// Shape-preserving fallback helpers
// ---------------------------------------------------------------------------

/** A 404 means "this route is not mounted on this server" — the signal this
 *  module falls back on. Any other status (400, 401, 409, 422, 5xx, ...)
 *  means the route exists and something else is wrong, so it must propagate
 *  unchanged rather than trigger a fallback that would mask it. */
export function isRouteMissing(error: unknown): boolean {
  return error instanceof CogneeApiError && error.status === 404;
}

/**
 * Resolve which recall route this base URL answers on, then run the actual
 * request. `run` receives the path to call and is responsible for parsing
 * *that* path's response shape (`/recall` and its legacy `/search` alias
 * share a request DTO but not a response one — see `types.ts`'s
 * `SearchResponseItem` doc — so only the caller, which knows both shapes,
 * can normalize correctly).
 *
 * - Cache already set: call the known-good path directly. No 404 is ever
 *   paid again for this base URL.
 * - Cache unset: try `/api/v1/recall`. A 404 falls back once to
 *   `/api/v1/search`; whichever responds (even with a non-404 error) decides
 *   the cached path, because a non-404 error still proves the route exists.
 */
export async function resolveRecallPath<T>(
  cache: CogneeCapabilities,
  run: (path: RecallPath) => Promise<T>,
): Promise<T> {
  if (cache.recallPath) return run(cache.recallPath);
  try {
    const result = await run("/api/v1/recall");
    cache.recallPath = "/api/v1/recall";
    return result;
  } catch (error) {
    if (isRouteMissing(error)) {
      const result = await run("/api/v1/search");
      cache.recallPath = "/api/v1/search";
      return result;
    }
    // Not a 404: /api/v1/recall exists (or the failure is unrelated to
    // routing, e.g. a network error) — cache it as the winner so a later
    // call doesn't re-attempt a fallback that was never warranted, then
    // let the real error propagate.
    cache.recallPath = "/api/v1/recall";
    throw error;
  }
}

/**
 * Same pattern as `resolveRecallPath`, for the login route's path prefix:
 * probe both, prefer the prefixed one, cache the winner.
 */
export async function resolveAuthPrefix<T>(
  cache: CogneeCapabilities,
  run: (prefix: AuthPrefix) => Promise<T>,
): Promise<T> {
  if (cache.authPrefix) return run(cache.authPrefix);
  try {
    const result = await run("/api/v1/auth");
    cache.authPrefix = "/api/v1/auth";
    return result;
  } catch (error) {
    if (isRouteMissing(error)) {
      const result = await run("/auth");
      cache.authPrefix = "/auth";
      return result;
    }
    cache.authPrefix = "/api/v1/auth";
    throw error;
  }
}
