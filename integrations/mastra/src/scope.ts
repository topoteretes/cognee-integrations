/**
 * Mastra id -> cognee scope mapping. Pure, no I/O — `client.ts` does the
 * actual `ensureDataset`/`remember`/`recall` calls.
 *
 * `CogneeMastraConfig.scope` (default `"tagged"`): one shared dataset with
 * per-thread `session_id` and `node_set` tags, recall narrowed by
 * `node_name: ["resource:<id>"]` (retrieval scoping, not access control);
 * `"dataset-per-resource"` uses `${datasetPrefix}${resourceId}` instead.
 */

import type { CogneeMastraConfig } from "./types.js";

export const DEFAULT_DATASET = "mastra";
export const DEFAULT_DATASET_PREFIX = "mastra_";
/** Placeholder resource suffix when `dataset-per-resource` is used without a resourceId. */
const NO_RESOURCE = "default";

/**
 * Percent-encodes everything outside `[A-Za-z0-9_-]` plus `.` (which
 * `encodeURIComponent` leaves alone); since `.` in the output only ever came from a literal `.` in
 * the input, this stays collision-free, and the result also has no space or `.`, satisfying
 * cognee's `check_dataset_name`.
 */
export function sanitizeId(id: string): string {
  return encodeURIComponent(id).replace(/\./g, "%2E");
}

/** Build one `node_set`/`node_name` tag: `"<kind>:<sanitized-id>"`. */
export function buildTag(kind: string, id: string): string {
  return `${kind}:${sanitizeId(id)}`;
}

/** `"resource:<resourceId>"` tag. */
export function resourceTag(resourceId: string): string {
  return buildTag("resource", resourceId);
}

/** `"thread:<threadId>"` tag. */
export function threadTag(threadId: string): string {
  return buildTag("thread", threadId);
}

/**
 * cognee `session_id` for a Mastra thread: `"mastra_<threadId>"`, unsanitized (opaque string,
 * cognee places no character restriction on it). Returns `""` for an absent threadId so callers can
 * do `if (sessionId)` without special-casing `undefined` vs `""`.
 */
export function sessionIdFor(threadId: string | undefined | null): string {
  return threadId ? `mastra_${threadId}` : "";
}

/** Config fields `scope.ts` reads — a narrow slice of `CogneeMastraConfig`. */
export type ScopeConfig = Pick<CogneeMastraConfig, "scope" | "dataset" | "datasetPrefix" | "nodeSet">;

/**
 * Dataset name for a resource under the configured scope mode: `"tagged"`
 * (default) always returns `config.dataset`; `"dataset-per-resource"`
 * returns `${datasetPrefix}${sanitize(resourceId)}`, falling back to `"default"` when no resourceId
 * is available.
 */
export function datasetNameForResource(config: ScopeConfig, resourceId?: string | null): string {
  if ((config.scope ?? "tagged") === "dataset-per-resource") {
    const prefix = config.datasetPrefix ?? DEFAULT_DATASET_PREFIX;
    const suffix = resourceId ? sanitizeId(resourceId) : NO_RESOURCE;
    return `${prefix}${suffix}`;
  }
  return config.dataset ?? DEFAULT_DATASET;
}

export interface ScopeIds {
  threadId?: string | null;
  resourceId?: string | null;
}

export interface ResolvedScope {
  /** dataset to read/write for this thread/resource, per the configured scope mode. */
  dataset: string;
  /** `mastra_<threadId>`, or `""` when no threadId is available. */
  sessionId: string;
  /**
   * node_set tags to attach on write — resource + thread + `config.nodeSet` extras, in that order.
   */
  nodeSet: string[];
  /**
   * node_name filter for recall: `["resource:<id>"]` when a resourceId is
   * available, else `["thread:<id>"]`, else `[]` when neither id is present.
   */
  nodeNameFilter: string[];
}

/**
 * Turn a Mastra thread/resource pair plus config into everything `client.ts` needs to scope one
 * `recall()`/`remember()` call.
 */
export function resolveScope(config: ScopeConfig, ids: ScopeIds): ResolvedScope {
  const dataset = datasetNameForResource(config, ids.resourceId);
  const sessionId = sessionIdFor(ids.threadId);

  const nodeSet: string[] = [];
  if (ids.resourceId) nodeSet.push(resourceTag(ids.resourceId));
  if (ids.threadId) nodeSet.push(threadTag(ids.threadId));
  if (config.nodeSet?.length) nodeSet.push(...config.nodeSet);

  const nodeNameFilter = ids.resourceId
    ? [resourceTag(ids.resourceId)]
    : ids.threadId
      ? [threadTag(ids.threadId)]
      : [];

  return { dataset, sessionId, nodeSet, nodeNameFilter };
}
