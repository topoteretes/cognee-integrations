import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
export const FIXTURES = join(here, "..", "fixtures", "cognee-1.6.5");

export function fixture(name) {
  return JSON.parse(readFileSync(join(FIXTURES, name), "utf8"));
}

export const json = (value, status = 200, headers = {}) =>
  new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json", ...headers } });

export const BASE_ENV = {
  COGNEE_BASE_URL: "http://cognee.test:8000",
  COGNEE_API_KEY: "ck_test_key_0123456789",
  LOVABLE_PROJECT_DATASET: "lovable_probe",
  LOVABLE_MCP_TOKEN: "lvm_test_token_0123456789abcdef",
  LOVABLE_RECEIPTS_PATH: ":memory:",
  LOVABLE_MCP_PORT: "0",
};

/**
 * A scripted stand-in for cognee 1.6.5. Every call is recorded; responses are
 * the captured fixtures unless a route override says otherwise.
 */
export function fakeCognee(overrides = {}) {
  const calls = [];
  const datasetId = "35c36577-01aa-5707-929c-0fee137c9031";
  const state = {
    datasets: [{ id: datasetId, name: "lovable_probe" }, { id: "9b8c7d6e-0000-4000-8000-000000000002", name: "shared_research" }],
    data: [],
    pipeline: "DATASET_PROCESSING_COMPLETED",
    recall: fixture("recall-chunks.json"),
  };
  const fetchImpl = async (url, init = {}) => {
    const u = new URL(url);
    const method = init.method ?? "GET";
    const headers = init.headers ?? {};
    const body = typeof init.body === "string" ? JSON.parse(init.body) : init.body;
    calls.push({ method, path: u.pathname + u.search, headers, body });
    const key = `${method} ${u.pathname}`;
    const override = overrides[key] ?? overrides[`${method} ${u.pathname}${u.search}`];
    if (override) return typeof override === "function" ? override({ method, url: u, body, headers, state }) : override;
    if (headers["X-Api-Key"] !== BASE_ENV.COGNEE_API_KEY && u.pathname !== "/health") return json(fixture("unauthorized.json").body, 401);
    switch (key) {
      case "GET /health":
        return json({ status: "ready", health: "healthy", version: "1.6.5" });
      case "GET /api/v1/datasets":
        return json(state.datasets);
      case "POST /api/v1/datasets": {
        const existing = state.datasets.find((d) => d.name === body.name);
        if (existing) return json(existing);
        const created = { id: `11111111-2222-4333-8444-${String(state.datasets.length).padStart(12, "0")}`, name: body.name };
        state.datasets.push(created);
        return json(created);
      }
      case "POST /api/v1/remember": {
        const file = body.get("data");
        const name = file.name.replace(/\.txt$/, "");
        const ds = state.datasets.find((d) => d.name === body.get("datasetName"));
        if (!state.data.find((d) => d.name === name)) {
          state.data.push({ id: `d03f564c-ff7b-4810-ac63-${String(state.data.length).padStart(12, "0")}`, name, datasetId: ds?.id ?? datasetId, text: await file.text() });
        }
        return json({ ...fixture("remember-background.json"), dataset_id: ds?.id ?? datasetId, dataset_name: body.get("datasetName") });
      }
      case "POST /api/v1/recall": {
        const missing = body.datasets.filter((name) => !state.datasets.some((d) => d.name === name));
        if (missing.length) return json({ detail: `Dataset(s) not found: '${missing.join("', '")}'. [DatasetNotFoundError]` }, 404);
        return json(typeof state.recall === "function" ? state.recall(body) : state.recall);
      }
      case "GET /api/v1/datasets/status":
        return json({ [u.searchParams.get("dataset")]: state.pipeline });
      case "POST /api/v1/forget": {
        const idx = state.data.findIndex((d) => d.id === body.dataId);
        if (idx >= 0) state.data.splice(idx, 1);
        return json({ data_id: body.dataId, dataset_id: body.datasetId, status: "success" });
      }
      default: {
        const m = /^GET \/api\/v1\/datasets\/([^/]+)\/data$/.exec(key);
        if (m) {
          const limit = Number(u.searchParams.get("limit") ?? 100);
          const offset = Number(u.searchParams.get("offset") ?? 0);
          return json(state.data.filter((d) => d.datasetId === m[1]).slice(offset, offset + limit).map(({ text, ...rest }) => rest));
        }
        return json({ detail: "not found" }, 404);
      }
    }
  };
  return { fetchImpl, calls, state, datasetId };
}
