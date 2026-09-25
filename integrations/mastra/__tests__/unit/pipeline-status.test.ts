import { extractPipelineStatus, isPipelineCompleted, isPipelineFailed } from "../../src/pipeline-status.js";

describe("isPipelineCompleted / isPipelineFailed", () => {
  it.each(["DATASET_PROCESSING_COMPLETED", "completed", "COMPLETED"])("%s counts as completed", (s) => {
    expect(isPipelineCompleted(s)).toBe(true);
    expect(isPipelineFailed(s)).toBe(false);
  });

  it.each(["DATASET_PROCESSING_ERRORED", "failed", "errored", "SOMETHING_FAILED"])("%s counts as failed", (s) => {
    expect(isPipelineFailed(s)).toBe(true);
    expect(isPipelineCompleted(s)).toBe(false);
  });

  it.each(["DATASET_PROCESSING_STARTED", "DATASET_PROCESSING_INITIATED", "running", "pending", "", undefined, null])(
    "%s is neither completed nor failed (still in flight / not started)",
    (s) => {
      expect(isPipelineCompleted(s)).toBe(false);
      expect(isPipelineFailed(s)).toBe(false);
    },
  );
});

describe("extractPipelineStatus", () => {
  it("reads the flat single-pipeline map a live server returns", () => {
    expect(extractPipelineStatus({ "ds-1": "DATASET_PROCESSING_STARTED" }, "ds-1")).toBe("DATASET_PROCESSING_STARTED");
  });

  it("reads the nested multi-pipeline shape", () => {
    expect(extractPipelineStatus({ "ds-1": { cognify_pipeline: "DATASET_PROCESSING_COMPLETED" } }, "ds-1")).toBe(
      "DATASET_PROCESSING_COMPLETED",
    );
  });

  it("returns undefined for a dataset with no run yet (empty map), not an error", () => {
    expect(extractPipelineStatus({}, "ds-1")).toBeUndefined();
    expect(extractPipelineStatus({ "ds-2": "DATASET_PROCESSING_COMPLETED" }, "ds-1")).toBeUndefined();
  });

  it("tolerates a non-object response", () => {
    expect(extractPipelineStatus(null, "ds-1")).toBeUndefined();
    expect(extractPipelineStatus("nope", "ds-1")).toBeUndefined();
  });
});
