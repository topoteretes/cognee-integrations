/**
 * Wire-format handling for the session layers and /improve:
 *   * normalizeSearchResults renders session/trace/session_context entries
 *     (which carry no `text`) and keeps the `source` discriminator;
 *   * renderSessionLayerSections groups them into labelled prompt blocks;
 *   * normalizeImproveResponse collapses both the flat and the per-dataset
 *     response shapes, so describeImprove never prints `status=?` for a map.
 */

import { normalizeImproveResponse, normalizeSearchResults } from "../../src/client";
import { MAX_ENTRIES_PER_LAYER, describeImprove, renderSessionLayerSections } from "../../src/recall-layers";

describe("normalizeSearchResults — recall sources", () => {
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

describe("renderSessionLayerSections", () => {
  it("groups by layer in guidance → trace → session order and skips graph entries", () => {
    const sections = renderSessionLayerSections([
      { id: "1", text: "Q: a\nA: b", score: 1, source: "session" },
      { id: "2", text: "Use --wait", score: 1, source: "session_context" },
      { id: "3", text: "graph hit", score: 0.9, source: "graph" },
      { id: "4", text: "deploy (error)", score: 1, source: "trace" },
    ]);
    expect(sections).toHaveLength(3);
    expect(sections[0]).toMatch(/^<agent_guidance>\n\[.*\]\n- Use --wait\n<\/agent_guidance>$/);
    expect(sections[1]).toMatch(/^<trace_lessons>/);
    expect(sections[2]).toBe("<session_memory>\n[Earlier turns of this conversation]\n- Q: a\n  A: b\n</session_memory>");
  });

  it("treats untagged entries as session Q&A, drops blanks, and caps each layer", () => {
    const many = Array.from({ length: MAX_ENTRIES_PER_LAYER + 3 }, (_, i) => ({ id: String(i), text: `turn ${i}`, score: 1 }));
    const sections = renderSessionLayerSections([...many, { id: "b", text: "   ", score: 1, source: "trace" as const }]);
    expect(sections).toHaveLength(1);
    expect(sections[0].split("\n- ")).toHaveLength(MAX_ENTRIES_PER_LAYER + 1);
  });

  it("returns [] when nothing is injectable", () => {
    expect(renderSessionLayerSections([])).toEqual([]);
    expect(renderSessionLayerSections([{ id: "g", text: "x", score: 1, source: "graph" }])).toEqual([]);
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

  it("tolerates garbage without throwing", () => {
    expect(normalizeImproveResponse(null)).toEqual({});
    expect(normalizeImproveResponse("nope")).toEqual({});
    expect(normalizeImproveResponse([1, 2])).toEqual({});
    expect(normalizeImproveResponse({ note: "no status here" })).toEqual({});
    expect(describeImprove(undefined)).toBe("status=?");
  });
});
