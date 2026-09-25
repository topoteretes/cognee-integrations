/**
 * `CogneeClient.datasetStatus()` and the recall-item text fallback, against
 * the real `node:http` mock server, using the response shapes a live
 * `cognee/cognee:main` 1.5.4 was observed to send.
 */

import { CogneeClient } from "../../src/client.js";
import { extractPipelineStatus, isPipelineCompleted } from "../../src/pipeline-status.js";
import { startMockCognee, MOCK_API_KEY, type MockCognee } from "../test-utils/mock-cognee.js";

let mock: MockCognee;

afterEach(async () => {
  await mock?.close();
});

describe("datasetStatus()", () => {
  it("builds the dataset/pipeline query and returns the server's flat enum-valued map", async () => {
    mock = await startMockCognee({
      routes: {
        "GET /api/v1/datasets/status": (req) => ({
          status: 200,
          body: { [String(req.query.dataset)]: "DATASET_PROCESSING_COMPLETED" },
        }),
      },
    });
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    const response = await client.datasetStatus("ds-42");

    const req = mock.requests.find((r) => r.path.startsWith("/api/v1/datasets/status"))!;
    expect(req.query.dataset).toBe("ds-42");
    expect(req.query.pipeline).toBe("cognify_pipeline");
    expect(extractPipelineStatus(response, "ds-42")).toBe("DATASET_PROCESSING_COMPLETED");
    expect(isPipelineCompleted(extractPipelineStatus(response, "ds-42"))).toBe(true);
  });

  it("returns an empty map (dataset absent) for a dataset with no run yet, without throwing", async () => {
    mock = await startMockCognee({
      routes: { "GET /api/v1/datasets/status": () => ({ status: 200, body: {} }) },
    });
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    const response = await client.datasetStatus("ds-42");
    expect(extractPipelineStatus(response, "ds-42")).toBeUndefined();
  });
});

describe("recall() item text fallback", () => {
  it("composes readable text for a session-cache item that has no `text` field", async () => {
    mock = await startMockCognee({
      routes: {
        "POST /api/v1/recall": () => ({
          status: 200,
          body: [
            { source: "session", question: "What is the capital?", answer: "Poseidonia", context: "Atlantis" },
            { source: "session_context", content: "Earlier the user mentioned Atlantis." },
            { source: "graph", text: "A plain chunk." },
          ],
        }),
      },
    });
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    const hits = await client.recall({ query: "capital", search_type: "CHUNKS", top_k: 3, only_context: true });

    expect(hits.map((h) => h.text)).toEqual([
      "Question: What is the capital?\nAnswer: Poseidonia\nContext: Atlantis",
      "Earlier the user mentioned Atlantis.",
      "A plain chunk.",
    ]);
    // None of them is a JSON dump of the record.
    for (const hit of hits) expect(hit.text.startsWith("{")).toBe(false);
  });
});
