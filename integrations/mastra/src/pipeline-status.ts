/**
 * Pipeline-run status values as a live cognee server reports them: GET
 * /api/v1/datasets/status returns the server's PipelineRunStatus enum name
 * per dataset (DATASET_PROCESSING_STARTED/COMPLETED/ERRORED on
 * cognee/cognee:main 1.5.4), while older docs and some bodies use lowercase
 * completed/failed. A dataset with no run under the requested pipeline is
 * absent from the map ({}) — treat that as "not started yet", not an error.
 */

export type PipelineStatusValue =
  | "DATASET_PROCESSING_INITIATED"
  | "DATASET_PROCESSING_STARTED"
  | "DATASET_PROCESSING_COMPLETED"
  | "DATASET_PROCESSING_ERRORED"
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | string;

/** True for any spelling of "the run finished successfully". */
export function isPipelineCompleted(status: string | undefined | null): boolean {
  const s = (status ?? "").toUpperCase();
  return s === "COMPLETED" || s.endsWith("_COMPLETED");
}

/** True for any spelling of "the run finished with an error". */
export function isPipelineFailed(status: string | undefined | null): boolean {
  const s = (status ?? "").toUpperCase();
  return s === "FAILED" || s === "ERRORED" || s.endsWith("_ERRORED") || s.endsWith("_FAILED");
}

/**
 * Read one dataset's status out of a `/datasets/status` response. Handles
 * the flat `{ [datasetId]: status }` map a single-pipeline query returns and
 * the nested `{ [datasetId]: { [pipeline]: status } }` shape of a
 * multi-pipeline query; `undefined` when the dataset has no run yet.
 */
export function extractPipelineStatus(response: unknown, datasetId: string): string | undefined {
  if (!response || typeof response !== "object") return undefined;
  const value = (response as Record<string, unknown>)[datasetId];
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const first = Object.values(value as Record<string, unknown>).find((v) => typeof v === "string");
    return typeof first === "string" ? first : undefined;
  }
  return undefined;
}
