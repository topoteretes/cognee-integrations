import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createTestHarness } from "@paperclipai/plugin-sdk/testing";
import { createPlugin, InMemoryLedger, manifest } from "../dist/lib/index.js";

const here = dirname(fileURLToPath(import.meta.url));
export const FIXTURES = join(here, "..", "fixtures", "cognee-1.6.5");

export function fixture(name) {
  return JSON.parse(readFileSync(join(FIXTURES, name), "utf8"));
}

export const json = (value, status = 200, headers = {}) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json", ...headers } });

export const COMPANY = "0d9f2a4e-7c1b-4b6e-9a3f-5e2d8c1a7b10";
export const OTHER_COMPANY = "ffffffff-0000-4000-8000-000000000099";
export const PROJECT = "6a1c3b2d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
export const PROJECT_2 = "7b2d4c3e-5f6a-4b7c-9d8e-0f1a2b3c4d5e";
export const PROJECT_OFF = "8c3e5d4f-6a7b-4c8d-ae9f-1a2b3c4d5e6f";
export const API_KEY = "ck_test_key_0123456789";

export const BASE_CONFIG = {
  cogneeBaseUrl: "http://cognee.test:8000",
  cogneeApiKeyRef: { type: "secret_ref", secretId: "secret-1" },
  enabledCompanyIds: [COMPANY],
  enabledProjectIds: [PROJECT, PROJECT_2],
  captureMode: "both",
  decisionMarker: "#remember",
  paperclipPublicUrl: "https://paperclip.example.com",
};

export function issue(overrides = {}) {
  return {
    id: "11111111-aaaa-4aaa-8aaa-111111111111",
    companyId: COMPANY,
    projectId: PROJECT,
    projectWorkspaceId: null,
    goalId: null,
    parentId: null,
    title: "Interview 12 onboarding drop-off customers",
    description: "Findings: 9 of 12 interviewed customers abandoned onboarding at the password step.",
    status: "done",
    workMode: "standard",
    priority: "medium",
    reviewPolicy: null,
    assigneeAgentId: "a1a1a1a1-1111-4111-8111-111111111111",
    assigneeUserId: null,
    checkoutRunId: null,
    executionRunId: null,
    executionAgentNameKey: null,
    executionLockedAt: null,
    createdByAgentId: null,
    createdByUserId: "u-1",
    responsibleUserId: null,
    issueNumber: 1,
    identifier: "RES-1",
    createdAt: new Date("2026-10-20T10:00:00Z"),
    updatedAt: new Date("2026-10-21T10:00:00Z"),
    ...overrides,
  };
}

export function comment(overrides = {}) {
  return {
    id: "c1c1c1c1-dddd-4ddd-8ddd-c1c1c1c1c1c1",
    companyId: COMPANY,
    issueId: "11111111-aaaa-4aaa-8aaa-111111111111",
    authorType: "agent",
    authorAgentId: "a2a2a2a2-2222-4222-8222-222222222222",
    authorUserId: null,
    body: "#remember Password friction is the leading onboarding drop-off cause.",
    presentation: null,
    metadata: null,
    createdAt: new Date("2026-10-21T11:00:00Z"),
    ...overrides,
  };
}

/**
 * A scripted stand-in for cognee 1.6.5 behind globalThis.fetch (the harness's
 * ctx.http.fetch delegates to global fetch, so the plugin's real outbound path
 * is exercised). Every call is recorded.
 */
export function fakeCognee(overrides = {}) {
  const calls = [];
  const state = { datasets: [], data: [], pipeline: "DATASET_PROCESSING_COMPLETED", recall: null, failRemember: null, failForget: false };
  const nextId = (prefix) => `${prefix}-${String(calls.length).padStart(4, "0")}`.padEnd(36, "0").replace(/^(.{8})(.{4})(.{4})(.{4})(.{12}).*$/, "$1-$2-4$3-8$4-$5".replace(/-4-4/, "-4")).slice(0, 36);
  const uuid = (n) => `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
  let counter = 1;
  const fetchImpl = async (url, init = {}) => {
    const u = new URL(url);
    const method = init.method ?? "GET";
    const headers = init.headers ?? {};
    const body = typeof init.body === "string" ? JSON.parse(init.body) : init.body;
    calls.push({ method, path: u.pathname + u.search, headers, body });
    const key = `${method} ${u.pathname}`;
    const override = overrides[key];
    if (override) return typeof override === "function" ? override({ method, url: u, body, headers, state }) : override;
    if (u.pathname !== "/health" && headers["X-Api-Key"] !== API_KEY) return json(fixture("unauthorized.json").body, 401);
    switch (key) {
      case "GET /health":
        return json({ status: "ready", health: "healthy", version: "1.6.5" });
      case "GET /api/v1/datasets":
        return json(state.datasets);
      case "POST /api/v1/datasets": {
        const existing = state.datasets.find((d) => d.name === body.name);
        if (existing) return json(existing);
        const created = { id: uuid(counter++), name: body.name };
        state.datasets.push(created);
        return json(created);
      }
      case "POST /api/v1/remember": {
        if (state.failRemember) return typeof state.failRemember === "function" ? state.failRemember() : state.failRemember;
        const file = body.get("data");
        const name = file.name.replace(/\.txt$/, "");
        const datasetName = body.get("datasetName");
        let ds = state.datasets.find((d) => d.name === datasetName);
        if (!ds) {
          ds = { id: uuid(counter++), name: datasetName };
          state.datasets.push(ds);
        }
        if (!state.data.find((d) => d.name === name && d.datasetId === ds.id)) state.data.push({ id: uuid(counter++), name, datasetId: ds.id, text: await file.text() });
        return json({ ...fixture("remember-background.json"), dataset_id: ds.id, dataset_name: datasetName });
      }
      case "POST /api/v1/recall": {
        const missing = body.datasets.filter((name) => !state.datasets.some((d) => d.name === name));
        if (missing.length) return json({ detail: `Dataset(s) not found: '${missing.join("', '")}'. [DatasetNotFoundError]` }, 404);
        if (state.recall) return json(typeof state.recall === "function" ? state.recall(body) : state.recall);
        const ids = new Set(state.datasets.filter((d) => body.datasets.includes(d.name)).map((d) => d.id));
        const hits = state.data.filter((d) => ids.has(d.datasetId)).filter((d) => body.query.split(/\s+/).some((word) => d.text.toLowerCase().includes(word.toLowerCase())));
        return json(hits.slice(0, body.top_k).map((d, i) => ({ kind: "chunk", search_type: "CHUNKS", text: d.text, score: 0.9 - i * 0.1, dataset_id: d.datasetId, dataset_name: state.datasets.find((x) => x.id === d.datasetId).name, metadata: { data_id: d.id, document_name: d.name } })));
      }
      case "GET /api/v1/datasets/status":
        return json({ [u.searchParams.get("dataset")]: state.pipeline });
      case "POST /api/v1/forget": {
        if (!state.failForget) {
          const idx = state.data.findIndex((d) => d.id === body.dataId);
          if (idx >= 0) state.data.splice(idx, 1);
        }
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
  void nextId;
  return { fetchImpl, calls, state };
}

/** Boot the plugin in the SDK harness with a fake cognee behind global fetch. */
export async function boot({ config = BASE_CONFIG, overrides = {}, seed = {}, clock, drainOnEvent } = {}) {
  const fake = fakeCognee(overrides);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = fake.fetchImpl;
  const harness = createTestHarness({ manifest, config });
  // The harness resolves secrets to "resolved:<ref>"; make it hand out the key the fake accepts.
  harness.ctx.secrets = { resolve: async () => API_KEY };
  harness.seed({ companies: [{ id: COMPANY, name: "Northwind", status: "active" }, { id: OTHER_COMPANY, name: "Other", status: "active" }], projects: [{ id: PROJECT, companyId: COMPANY, name: "Research", status: "active" }, { id: PROJECT_2, companyId: COMPANY, name: "Product", status: "active" }, { id: PROJECT_OFF, companyId: COMPANY, name: "Marketing", status: "active" }], ...seed });
  const ledger = new InMemoryLedger(clock);
  const created = createPlugin({ ledger, clock, drainOnEvent });
  await created.plugin.definition.setup(harness.ctx);
  const restore = () => {
    globalThis.fetch = originalFetch;
  };
  return { harness, fake, ledger, plugin: created.plugin, runtime: created.runtime(), restore };
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
