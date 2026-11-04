/**
 * Wire-format handling for recall items and /improve:
 *   * normalizeSearchResults keeps a graph item's `text` verbatim (the cognee
 *     >= 1.6.0 full-prompt text included) and never reads `system_prompt`;
 *     session/trace/session_context entries (which carry no `text`; no
 *     search path requests them any more, but the client stays wire-complete)
 *     are rendered from their fields and keep the `source` discriminator;
 *   * normalizeImproveResponse collapses both the flat and the per-dataset
 *     response shapes, so describeImprove never prints `status=?` for a map.
 */

import { normalizeImproveResponse, normalizeSearchResults } from "../../src/client";
import { describeImprove } from "../../src/recall-layers";

const FULL_PROMPT_TEXT = [
  "User: what theme do I like?",
  "Assistant: dark mode",
  "",
  "The question is: `what did we decide about the theme?`",
  "Answer using only the context below.",
  "Context:",
  "`User prefers dark mode. Deploys happen on Fridays.`",
  "",
  "Session guidance: always confirm before deleting.",
].join("\n");

describe("normalizeSearchResults — recall sources", () => {
  it("keeps a cognee >= 1.6.0 graph item's full-prompt text whole and ignores system_prompt", () => {
    const [r] = normalizeSearchResults([
      { source: "graph", dataset_id: "ds-1", text: FULL_PROMPT_TEXT, system_prompt: "Answer the question using the provided context. Be as brief as possible.", content: "should not win", search_result: ["should not win either"] },
    ]);
    expect(r).toEqual({ id: "ds-1", text: FULL_PROMPT_TEXT, score: 1, metadata: undefined, source: "graph" });
    expect(r.text).not.toContain("Be as brief as possible");
    expect(r).not.toHaveProperty("system_prompt");
  });

  it("keeps an older server's bare graph context exactly as sent", () => {
    const [r] = normalizeSearchResults([{ source: "graph", text: "bare context" }]);
    expect(r.text).toBe("bare context");
    expect(r.source).toBe("graph");
  });

  it("renders a session Q&A entry and tags its source", () => {
    const [r] = normalizeSearchResults([
      { source: "session", question: "what theme?", answer: "dark", context: "", feedback_text: "correct", entry_id: "qa-1" },
    ]);
    expect(r).toMatchObject({ id: "qa-1", source: "session", score: 1 });
    expect(r.text).toBe("Q: what theme?\nA: dark\nFeedback: correct");
  });

  it("renders a trace entry with function, status, params, return and lesson", () => {
    const [r] = normalizeSearchResults([
      { source: "trace", origin_function: "deploy", status: "error", method_params: { env: "prod" }, return_value: "timeout", feedback_text: "retry with --wait" },
    ]);
    expect(r.source).toBe("trace");
    expect(r.text).toBe('deploy (error) params={"env":"prod"}\nreturned: timeout\nLesson: retry with --wait');
  });

  it("renders session_context content and passes graph entries through unchanged", () => {
    const rs = normalizeSearchResults([
      { source: "session_context", content: "Always confirm before deleting.", context_profile: "agent" },
      { source: "graph", id: "g1", text: "User prefers dark mode", score: 0.9, metadata: { a: 1 } },
    ]);
    expect(rs[0]).toMatchObject({ source: "session_context", text: "Always confirm before deleting." });
    expect(rs[1]).toEqual({ id: "g1", text: "User prefers dark mode", score: 0.9, metadata: { a: 1 }, source: "graph" });
  });

  it("accepts the legacy _source key, ignores unknown sources, and keeps old shapes working", () => {
    const rs = normalizeSearchResults([
      { _source: "session", question: "q", answer: "a" },
      { source: "bogus", text: "x" },
      "plain string",
      { search_result: ["cloud", "format"] },
    ]);
    expect(rs[0].source).toBe("session");
    expect(rs[1].source).toBeUndefined();
    expect(rs[2].text).toBe("plain string");
    expect(rs[3].text).toBe("cloud\nformat");
  });
});

describe("normalizeImproveResponse / describeImprove", () => {
  it("keeps the legacy flat shape", () => {
    const r = normalizeImproveResponse({ status: "ok", pipeline_run_id: "run-1", dataset_id: "ds-1" });
    expect(r).toEqual({ status: "ok", pipelineRunId: "run-1", datasetId: "ds-1" });
    expect(describeImprove(r)).toBe("status=ok run=run-1");
  });

  it("unwraps a single-dataset map (cognee >= 1.4)", () => {
    const r = normalizeImproveResponse({
      "2923db6a-4d89-5429-bac8-b9db95fab01b": { status: "PipelineRunCompleted", pipeline_run_id: "b5ded752-aaaa", dataset_id: "2923db6a" },
    });
    expect(r).toEqual({
      status: "PipelineRunCompleted",
      pipelineRunId: "b5ded752-aaaa",
      datasetId: "2923db6a-4d89-5429-bac8-b9db95fab01b",
      datasets: { "2923db6a-4d89-5429-bac8-b9db95fab01b": { status: "PipelineRunCompleted", pipelineRunId: "b5ded752-aaaa" } },
    });
    expect(describeImprove(r)).toBe("status=PipelineRunCompleted run=b5ded752");
  });

  it("summarizes a multi-dataset map as mixed when statuses differ", () => {
    const r = normalizeImproveResponse({
      a: { status: "PipelineRunCompleted", pipeline_run_id: "r1" },
      b: { status: "PipelineRunStarted", pipeline_run_id: "r2" },
    });
    expect(r.status).toBe("mixed");
    expect(r.datasetId).toBeUndefined();
    expect(Object.keys(r.datasets ?? {})).toEqual(["a", "b"]);
    expect(describeImprove(r)).toBe("status=mixed datasets=2");

    const same = normalizeImproveResponse({ a: { status: "PipelineRunCompleted" }, b: { status: "PipelineRunCompleted" } });
    expect(same.status).toBe("PipelineRunCompleted");
  });

  it("names an unrecognized shape in `error` instead of silently returning {}", () => {
    expect(normalizeImproveResponse(null)).toEqual({ error: "unexpected improve response: null" });
    expect(normalizeImproveResponse("nope")).toEqual({ error: "unexpected improve response: string" });
    expect(normalizeImproveResponse([1, 2])).toEqual({ error: "unexpected improve response: array" });
    expect(normalizeImproveResponse({ note: "no status here", other: 1 })).toEqual({ error: "unexpected improve response: object with keys [note, other]" });
    expect(describeImprove(undefined)).toBe("status=?");
    expect(describeImprove(normalizeImproveResponse("nope"))).toBe("status=? (unexpected improve response: string)");
  });
});
