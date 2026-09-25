/**
 * Live smoke test — the only file here that talks to a real cognee server.
 * Excluded from `npm test`; run explicitly with `COGNEE_LIVE=1 npm run
 * test:live` against a server from `examples/docker-compose.smoke.yml` (or
 * `COGNEE_LIVE_URL` elsewhere).
 *
 * Sequence: health -> ensureDataset -> remember -> poll status+recall until
 * recallable (cap 180s) -> forget.
 */

import { CogneeClient } from "../../src/client.js";
import { getCapabilities } from "../../src/capabilities.js";
import { extractPipelineStatus, isPipelineCompleted, isPipelineFailed } from "../../src/pipeline-status.js";

const LIVE = process.env.COGNEE_LIVE === "1";
const BASE_URL = process.env.COGNEE_LIVE_URL || "http://localhost:8000";

const POLL_INTERVAL_MS = 2_000;
/** Cap 180s, then fail with a clear message. */
const COGNIFY_POLL_CAP_MS = 180_000;
/**
 * Generous — real network calls to a real LLM-backed pipeline, not the mock server's near-instant
 * responses.
 */
const TEST_TIMEOUT_MS = COGNIFY_POLL_CAP_MS + 60_000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

describe("cognee live smoke test", () => {
  if (!LIVE) {
    test.skip("skipped — set COGNEE_LIVE=1 (and optionally COGNEE_LIVE_URL) to run against a real cognee server", () => {
      // intentionally empty — test.skip never executes this body.
    });
    return;
  }

  it(
    "completes the full ingest -> cognify -> recall -> forget cycle against a real server",
    async () => {
      // Auth: cognee/cognee:main (cognee 1.5.4) enforces auth on the API
      // routes regardless of REQUIRE_AUTHENTICATION, and rejects arbitrary
      // X-Api-Key values, so the stock-server path is a JWT login as the
      // built-in default user. COGNEE_API_KEY (if set) still wins for
      // servers configured with API-key auth.
      const client = new CogneeClient({
        baseUrl: BASE_URL,
        ...(process.env.COGNEE_API_KEY
          ? { apiKey: process.env.COGNEE_API_KEY }
          : {
              auth: {
                email: process.env.COGNEE_USER_EMAIL ?? "default_user@example.com",
                password: process.env.COGNEE_USER_PASSWORD ?? "default_password",
              },
            }),
      });

      // 1. GET /health — also the live-test gate, per client.ts's own doc comment.
      const health = await client.health();
      console.log("[live-smoke] GET /health ->", health);
      expect(health.status).toBeDefined();
      if (health.status && health.status !== "ready") {
        throw new Error(
          `cognee server at ${BASE_URL} reported health status "${health.status}" (reason: ${health.reason ?? "none given"}) — aborting before writing any data.`,
        );
      }

      // 2. ensureDataset — a fresh name per run so repeated live runs never collide.
      const datasetName = `mastra-live-smoke-${Date.now()}`;
      const dataset = await client.ensureDataset(datasetName);
      console.log("[live-smoke] ensureDataset ->", dataset);
      expect(dataset.id).toBeTruthy();

      // 3. remember a distinctive sentence.
      const marker = `live-smoke-marker-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const sentence = `The distinctive smoke-test fact for this run is: ${marker}.`;
      const sessionId = `live-smoke-session-${Date.now()}`;
      const rememberedAt = Date.now();
      const rememberResult = await client.remember({
        raw_data: [sentence],
        datasetId: dataset.id,
        session_id: sessionId,
        run_in_background: true,
      });
      console.log("[live-smoke] remember ->", rememberResult);

      // 4. wait until the sentence is recallable (cap 180s), watching both
      // the cognify pipeline status and recall itself. On cognee 1.5.4 a
      // session-mode remember() answers session_stored immediately with no
      // pipeline run for a while, then a background bridge runs cognify —
      // recall finding the marker is the success criterion; status is only logged.
      const recallMarker = async () => {
        const hits = await client.recall({
          query: marker,
          search_type: "CHUNKS",
          dataset_ids: [dataset.id],
          top_k: 5,
          only_context: true,
        });
        return { hits, found: hits.some((hit) => hit.text.includes(marker)) };
      };

      const deadline = rememberedAt + COGNIFY_POLL_CAP_MS;
      let lastStatus: string | undefined;
      let completedAt: number | undefined;
      let recalled: Awaited<ReturnType<typeof recallMarker>> | undefined;
      while (Date.now() < deadline) {
        const statusResponse = await client.datasetStatus(dataset.id);
        lastStatus = extractPipelineStatus(statusResponse, dataset.id);
        console.log("[live-smoke] datasetStatus ->", lastStatus ?? statusResponse);
        if (isPipelineFailed(lastStatus)) {
          throw new Error(`cognee's cognify pipeline reported "${lastStatus}" for dataset ${dataset.id} (${datasetName}).`);
        }
        if (isPipelineCompleted(lastStatus) && completedAt === undefined) completedAt = Date.now();

        try {
          const attempt = await recallMarker();
          if (attempt.found) {
            recalled = attempt;
            break;
          }
        } catch (error) {
          // "No data found" (404-ish) until the first cognify lands — expected while waiting.
          console.log("[live-smoke] recall not ready yet:", error instanceof Error ? error.message : String(error));
        }
        await sleep(POLL_INTERVAL_MS);
      }
      const recallableDelayMs = Date.now() - rememberedAt;
      if (!recalled) {
        throw new Error(
          `the remembered sentence did not become recallable within ${COGNIFY_POLL_CAP_MS}ms for dataset ` +
            `${dataset.id} (${datasetName}) — last observed pipeline status: ${lastStatus ?? "none"}. Check the server logs.`,
        );
      }
      // The write path is eventually consistent — an observation from this
      // run, not a guarantee this package makes anywhere else.
      console.log(
        `[live-smoke] ingest -> recallable delay observed this run: ~${recallableDelayMs}ms` +
          (completedAt !== undefined
            ? ` (cognify_pipeline completed at ~${completedAt - rememberedAt}ms)`
            : ` (cognify_pipeline status at that moment: ${lastStatus ?? "no run for this dataset"})`),
      );

      // 5. the recall that found it.
      const hits = recalled.hits;
      console.log(
        "[live-smoke] recall hits ->",
        hits.map((hit) => hit.text),
      );
      expect(recalled.found).toBe(true);

      // 6. forget(memory_only: true) — cleans up this run's data; never `everything: true`.
      const forgetResult = await client.forget({ dataset_id: dataset.id, memory_only: true });
      console.log("[live-smoke] forget ->", forgetResult);

      // Print the resolved capability probes for whichever server this run
      // hit — paste into the PR description alongside the server version
      // (health.version, README compatibility table).
      const caps = getCapabilities(client.baseUrl);
      console.log("[live-smoke] resolved capability probes:", {
        serverVersion: health.version,
        recallPath: caps.recallPath,
        rememberSupported: caps.rememberSupported,
        rememberEntrySupported: caps.rememberEntrySupported,
        authPrefix: caps.authPrefix,
        ingestToRecallableDelayMs: recallableDelayMs,
      });
    },
    TEST_TIMEOUT_MS,
  );
});
