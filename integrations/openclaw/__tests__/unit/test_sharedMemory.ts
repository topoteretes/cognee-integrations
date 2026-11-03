/**
 * Shared agent memory for the provisioned "openclaw" identity (SDK-646).
 *
 * Under `pluginIdentity: "auto"` (the default) the plugin provisions its own
 * agent only in service of shared agent memory: the agent joins the user's
 * tenant and `cognee-agent` role, the role is granted the user's datasets, and
 * every dataset is addressed by the UUID of a canonical, parent-owned copy.
 * When that wiring cannot be completed the freshly minted identity is
 * reverted — its key revoked, the cache cleared — so the gateway runs as the
 * principal and nothing the user owns is ever stranded behind an agent that
 * cannot reach it. Mirrors `ensure_shared_memory` in the Claude Code plugin.
 */

import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CogneeHttpClient, CogneeRequestError } from "../../src/client";
import { resolveConfig } from "../../src/config";
import {
  PLUGIN_KEY,
  canonicalDatasetId,
  fingerprint,
  resolveSessionIdentity,
  resolveSharedDataset,
  wireSharedMemory,
  type IdentitySession,
  type ProvisionResult,
} from "../../src/plugin-identity";
import { ensureSharedMemory, sharedMemoryEnabled, type ControlPlane } from "../../src/shared-memory";

const BASE_URL = "http://127.0.0.1:8011";
const PRINCIPAL = "principal-key";
const AGENT_KEY = "agentkey-1";
const AGENT_ID = "agent-uuid-1";
const PARENT_ID = "parent-uuid-1";
const DATASET = "agent_sessions";
const PROVISIONED: ProvisionResult = { apiKey: AGENT_KEY, agentId: AGENT_ID, keyId: "k1", created: true };
const PARENT_DS = { id: "ds-1", name: DATASET, owner_id: PARENT_ID, created_at: "2026-01-01T00:00:00Z" };
const AGENT_DS = { id: "ds-2", name: DATASET, owner_id: AGENT_ID, created_at: "2026-02-01T00:00:00Z" };

type Reply = { status: number; body?: unknown };
type Script = Record<string, Reply | Reply[]>;

/** A scripted server: responses keyed by `"METHOD /path"` (query dropped), arrays consumed in order. */
class FakeServer {
  calls: { method: string; path: string; body?: unknown; asKey: string }[] = [];
  constructor(private script: Script) {}

  plane(asKey: string): ControlPlane {
    return async (method, path, body) => {
      this.calls.push({ method, path, body, asKey });
      const key = `${method} ${path.split("?")[0]}`;
      const entry = this.script[key];
      if (entry === undefined) return { status: 404, body: { detail: `no route ${key}` } };
      const reply = Array.isArray(entry) ? (entry.length > 1 ? entry.shift()! : entry[0]) : entry;
      return { status: reply.status, body: reply.body };
    };
  }
  callsTo(method: string, path: string) {
    return this.calls.filter((c) => c.method === method && c.path.split("?")[0] === path);
  }
  call(method: string, path: string) {
    const found = this.callsTo(method, path);
    expect(found).toHaveLength(1);
    return found[0];
  }
}

function freshScript(overrides: Script = {}): Script {
  return {
    "GET /api/v1/permissions/tenants/me": { status: 200, body: {} },
    "GET /api/v1/users/me": { status: 200, body: { id: PARENT_ID, tenant_id: "" } },
    "GET /api/v1/datasets": [{ status: 200, body: [] }, { status: 200, body: [] }, { status: 200, body: [PARENT_DS] }],
    "POST /api/v1/permissions/tenants": { status: 200, body: { tenant_id: "t-1" } },
    "GET /api/v1/permissions/tenants/t-1/roles": { status: 200, body: [] },
    "POST /api/v1/permissions/roles": { status: 200, body: { role_id: "r-1" } },
    [`POST /api/v1/permissions/users/${AGENT_ID}/tenants`]: { status: 200, body: {} },
    "POST /api/v1/permissions/tenants/select": { status: 200, body: {} },
    [`POST /api/v1/permissions/users/${AGENT_ID}/roles`]: { status: 200, body: {} },
    "POST /api/v1/datasets": { status: 200, body: { id: "ds-1", name: DATASET } },
    "POST /api/v1/permissions/datasets/r-1": { status: 200, body: {} },
    [`DELETE /api/v1/permissions/users/${AGENT_ID}/roles`]: { status: 200, body: {} },
    ...overrides,
  };
}

/** The identity client the policy drives: provisioning plus the control plane, backed by one FakeServer. */
class FakeClient {
  keys: string[] = [];
  additive = true;
  provisionOutcome: ProvisionResult | Error = PROVISIONED;
  provisionCalls: { asKey: string }[] = [];
  revoked: { plugin: string; keyId: string }[] = [];
  constructor(readonly server: FakeServer, private current = PRINCIPAL) {}
  setApiKey(key: string) {
    this.current = key;
    this.keys.push(key);
  }
  async additivePluginKeysSupported() {
    return this.additive;
  }
  async provisionPlugin(): Promise<ProvisionResult> {
    this.provisionCalls.push({ asKey: this.current });
    if (this.provisionOutcome instanceof Error) throw this.provisionOutcome;
    return this.provisionOutcome;
  }
  controlPlane(apiKey: string) {
    return this.server.plane(apiKey);
  }
  async revokePluginKey(plugin: string, keyId: string) {
    this.revoked.push({ plugin, keyId });
    return { found: true };
  }
}

let dir: string;
let cachePath: string;
let markerPath: string;
const env = process.env.COGNEE_SHARED_AGENT_MEMORY;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "cognee-openclaw-shared-"));
  cachePath = join(dir, "agent_key.json");
  markerPath = join(dir, "shared_memory.json");
  delete process.env.COGNEE_SHARED_AGENT_MEMORY;
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
  if (env === undefined) delete process.env.COGNEE_SHARED_AGENT_MEMORY;
  else process.env.COGNEE_SHARED_AGENT_MEMORY = env;
});

const readMarker = () => JSON.parse(readFileSync(markerPath, "utf-8")) as Record<string, unknown>;
const session = (mode: IdentitySession["mode"] = "auto", extra: Partial<IdentitySession> = {}): IdentitySession => ({
  baseUrl: BASE_URL,
  principalKey: PRINCIPAL,
  mode,
  cachePath,
  markerPath,
  sharedMemoryEnabled: true,
  ...extra,
});
const seedCache = () => {
  mkdirSync(dir, { recursive: true });
  writeFileSync(cachePath, JSON.stringify({ base_url: BASE_URL, api_key: AGENT_KEY, agent_id: AGENT_ID, key_id: "k1", plugin_key: PLUGIN_KEY, principal_fingerprint: fingerprint(PRINCIPAL) }));
};

/** Provision + wire, the way `activateApiKey` in plugin.ts does on gateway start. */
async function start(client: FakeClient, s: IdentitySession) {
  await resolveSessionIdentity(client, s);
  await wireSharedMemory(client, s);
  if (s.identity) client.setApiKey(s.identity.apiKey);
  return s;
}

describe("sharedMemoryEnabled", () => {
  it("defaults on, config can opt out, env wins", () => {
    expect(sharedMemoryEnabled(undefined)).toBe(true);
    expect(sharedMemoryEnabled(false)).toBe(false);
    process.env.COGNEE_SHARED_AGENT_MEMORY = "false";
    expect(sharedMemoryEnabled(true)).toBe(false);
    process.env.COGNEE_SHARED_AGENT_MEMORY = "true";
    expect(sharedMemoryEnabled(false)).toBe(true);
  });
  it("is resolved into the plugin config", () => {
    expect(resolveConfig({}).sharedAgentMemory).toBe(true);
    expect(resolveConfig({ sharedAgentMemory: false }).sharedAgentMemory).toBe(false);
  });
});

describe("auto: provision for shared memory, wire, address by UUID", () => {
  it("fresh install provisions, wires as the principal, and resolves the canonical dataset", async () => {
    const server = new FakeServer(freshScript());
    const client = new FakeClient(server);
    const s = await start(client, session());

    expect(client.provisionCalls).toEqual([{ asKey: PRINCIPAL }]);
    expect(s.identity?.apiKey).toBe(AGENT_KEY);
    expect(s.shared?.mode).toBe("shared");
    // Tenant created (tenant-less parent, no datasets), role created, agent
    // made a member — the control plane as the PRINCIPAL, the tenant selection
    // as the AGENT itself.
    const tenant = server.call("POST", "/api/v1/permissions/tenants");
    expect(tenant.asKey).toBe(PRINCIPAL);
    expect(tenant.path).toContain("tenant_name=cognee-parent-u");
    expect(server.call("POST", "/api/v1/permissions/roles").asKey).toBe(PRINCIPAL);
    expect(server.call("POST", `/api/v1/permissions/users/${AGENT_ID}/tenants`).path).toContain("tenant_id=t-1");
    const select = server.call("POST", "/api/v1/permissions/tenants/select");
    expect(select.asKey).toBe(AGENT_KEY);
    expect(select.body).toEqual({ tenant_id: "t-1" });
    expect(server.call("POST", `/api/v1/permissions/users/${AGENT_ID}/roles`).path).toContain("role_id=r-1");
    expect(readMarker()).toMatchObject({ mode: "shared", tenant_id: "t-1", role_id: "r-1", parent_user_id: PARENT_ID, agent_id: AGENT_ID });
    expect(client.keys.at(-1)).toBe(AGENT_KEY);

    // The dataset: created as the PRINCIPAL, granted read+write to the role.
    await expect(resolveSharedDataset(client, s, DATASET)).resolves.toBe("ds-1");
    expect(server.call("POST", "/api/v1/datasets").asKey).toBe(PRINCIPAL);
    const grants = server.callsTo("POST", "/api/v1/permissions/datasets/r-1");
    expect(grants.map((g) => g.path.split("permission_name=")[1]).sort()).toEqual(["read", "write"]);
    expect(grants.every((g) => Array.isArray(g.body) && g.body[0] === "ds-1")).toBe(true);
    expect(canonicalDatasetId(s, DATASET)).toBe("ds-1");
    expect(readMarker().canonical).toEqual({ [DATASET]: "ds-1" });
    // Resolved once: the second ask is answered from memory.
    await expect(resolveSharedDataset(client, s, DATASET)).resolves.toBe("ds-1");
    expect(server.callsTo("POST", "/api/v1/datasets")).toHaveLength(1);
  });

  it("a restart reuses the wiring and the canonical dataset without setting them up again", async () => {
    await resolveSharedDataset(new FakeClient(new FakeServer(freshScript())), await start(new FakeClient(new FakeServer(freshScript())), session()), DATASET);
    const server = new FakeServer(freshScript({ "GET /api/v1/datasets": { status: 200, body: [PARENT_DS] } }));
    const client = new FakeClient(server);
    const s = await start(client, session());
    expect(client.provisionCalls).toEqual([]);
    expect(s.identity?.apiKey).toBe(AGENT_KEY);
    expect(s.shared?.mode).toBe("shared");
    expect(server.callsTo("POST", "/api/v1/permissions/tenants")).toEqual([]);
    expect(server.callsTo("POST", "/api/v1/permissions/roles")).toEqual([]);
    expect(server.callsTo("POST", "/api/v1/permissions/datasets/r-1")).toEqual([]); // grant memoised
    expect(canonicalDatasetId(s, DATASET)).toBe("ds-1"); // from the marker, no network
    expect(server.callsTo("POST", "/api/v1/datasets")).toEqual([]);
  });

  it("prefers the parent-owned copy as canonical and keeps the agent's own copy readable", async () => {
    const server = new FakeServer(
      freshScript({
        "GET /api/v1/users/me": { status: 200, body: { id: PARENT_ID, tenant_id: "t-1" } },
        "GET /api/v1/datasets": { status: 200, body: [PARENT_DS, AGENT_DS] },
        "GET /api/v1/permissions/tenants/t-1/roles": { status: 200, body: [{ id: "r-1", name: "cognee-agent" }] },
        "POST /api/v1/permissions/datasets/r-1": [{ status: 200 }, { status: 200 }, { status: 403 }, { status: 403 }],
      }),
    );
    const client = new FakeClient(server);
    const s = await start(client, session());
    const outcome = await ensureSharedMemory(client.controlPlane(PRINCIPAL), client.controlPlane(AGENT_KEY), { baseUrl: BASE_URL, agentId: AGENT_ID, dataset: DATASET, markerPath });
    expect(outcome).toMatchObject({ mode: "shared", datasetId: "ds-1", datasetIds: ["ds-1", "ds-2"] });
    expect(server.callsTo("POST", "/api/v1/datasets")).toEqual([]);
    expect(s.shared?.mode).toBe("shared");
  });
});

describe("auto: revert when shared memory cannot be wired", () => {
  it("a server without the permissions API reverts the fresh identity", async () => {
    const server = new FakeServer(freshScript({ "GET /api/v1/permissions/tenants/me": { status: 404 } }));
    const client = new FakeClient(server);
    const s = await start(client, session());
    expect(client.provisionCalls).toHaveLength(1); // it did try
    expect(client.revoked).toEqual([{ plugin: PLUGIN_KEY, keyId: "k1" }]); // only this machine's key
    expect(s.identity).toBeUndefined();
    expect(client.keys).not.toContain(AGENT_KEY);
    expect(existsSync(cachePath)).toBe(false);
    expect(readMarker().reason).toBe("unsupported");
    await expect(resolveSharedDataset(client, s, DATASET)).resolves.toBeUndefined();
  });

  it("a structural failure is not retried on the same plugin version", async () => {
    const bad = { "GET /api/v1/permissions/tenants/me": { status: 404 } };
    await start(new FakeClient(new FakeServer(freshScript(bad))), session());
    const client = new FakeClient(new FakeServer(freshScript(bad)));
    await start(client, session());
    expect(client.provisionCalls).toEqual([]);
  });

  it("a tenant-less parent that already owns data stays on the principal", async () => {
    const server = new FakeServer(freshScript({ "GET /api/v1/datasets": { status: 200, body: [PARENT_DS] } }));
    const client = new FakeClient(server);
    const s = await start(client, session());
    expect(s.identity).toBeUndefined();
    expect(readMarker().reason).toBe("tenantless_with_data");
    expect(server.callsTo("POST", "/api/v1/permissions/tenants")).toEqual([]);
    expect(client.revoked).toHaveLength(1);
  });

  it("an org member who cannot create roles stays on the principal", async () => {
    const server = new FakeServer(
      freshScript({
        "GET /api/v1/users/me": { status: 200, body: { id: PARENT_ID, tenant_id: "t-shared" } },
        "GET /api/v1/permissions/tenants/t-shared/roles": { status: 200, body: [] },
        "POST /api/v1/permissions/roles": { status: 403 },
      }),
    );
    const client = new FakeClient(server);
    const s = await start(client, session());
    expect(s.identity).toBeUndefined();
    expect(readMarker().reason).toBe("not_tenant_owner");
  });

  it("shared memory opted out provisions nothing", async () => {
    const client = new FakeClient(new FakeServer(freshScript()));
    const s = await start(client, session("auto", { sharedMemoryEnabled: false }));
    expect(client.provisionCalls).toEqual([]);
    expect(s.identity).toBeUndefined();
  });

  it("a cached identity of another principal is left alone", async () => {
    mkdirSync(dir, { recursive: true });
    writeFileSync(cachePath, JSON.stringify({ base_url: BASE_URL, api_key: AGENT_KEY, agent_id: AGENT_ID, principal_fingerprint: fingerprint("someone-else") }));
    const client = new FakeClient(new FakeServer(freshScript()));
    const s = await start(client, session());
    expect(client.provisionCalls).toEqual([]);
    expect(s.identity).toBeUndefined();
    expect(existsSync(cachePath)).toBe(true);
  });
});

describe("explicit identity: kept whatever shared memory does", () => {
  it("true keeps the identity when shared memory is unsupported", async () => {
    const client = new FakeClient(new FakeServer(freshScript({ "GET /api/v1/permissions/tenants/me": { status: 404 } })));
    const s = await start(client, session("enabled"));
    expect(s.identity?.apiKey).toBe(AGENT_KEY);
    expect(client.revoked).toEqual([]);
    expect(s.shared?.mode).toBe("separated");
    await expect(resolveSharedDataset(client, s, DATASET)).resolves.toBeUndefined(); // by name
  });

  it("true throws when provisioning is impossible", async () => {
    const client = new FakeClient(new FakeServer(freshScript()));
    client.provisionOutcome = new CogneeRequestError(409, "full");
    await expect(start(client, session("enabled"))).rejects.toThrow(/openclaw cognee keys/);
  });
});

describe("opt-out with an existing identity", () => {
  it("leaves the role, keeps the agent, and addresses by name", async () => {
    await resolveSharedDataset(new FakeClient(new FakeServer(freshScript())), await start(new FakeClient(new FakeServer(freshScript())), session()), DATASET);
    seedCache();
    const server = new FakeServer(freshScript());
    const client = new FakeClient(server);
    const s = await start(client, session("auto", { sharedMemoryEnabled: false }));
    const leave = server.call("DELETE", `/api/v1/permissions/users/${AGENT_ID}/roles`);
    expect(leave.asKey).toBe(PRINCIPAL);
    expect(leave.path).toContain("role_id=r-1");
    expect(s.identity?.apiKey).toBe(AGENT_KEY); // the identity stays; its memory is its own now
    expect(s.shared).toMatchObject({ mode: "separated", reason: "opt_out" });
    expect(readMarker()).toMatchObject({ mode: "separated", role_id: "r-1" }); // kept, so re-enabling rejoins
    expect(canonicalDatasetId(s, DATASET)).toBeUndefined();
  });
});

describe("CogneeHttpClient.controlPlane", () => {
  const originalFetch = global.fetch;
  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("authenticates as the given key, never throws, and reports the status", async () => {
    const fetchMock = jest.fn(async (_url: string, init: RequestInit) => {
      const headers = init.headers as Record<string, string>;
      if (headers["X-Api-Key"] !== PRINCIPAL) return new Response("{}", { status: 401 });
      return new Response(JSON.stringify({ tenant_id: "t-1" }), { status: 201, headers: { "Content-Type": "application/json" } });
    }) as unknown as typeof fetch;
    global.fetch = fetchMock;
    const client = new CogneeHttpClient("http://cognee.test", AGENT_KEY); // the client runs as the agent...
    const asPrincipal = client.controlPlane(PRINCIPAL); // ...the control plane as the principal
    await expect(asPrincipal("POST", "/api/v1/permissions/tenants?tenant_name=x")).resolves.toEqual({ status: 200, body: { tenant_id: "t-1" } });
    await expect(client.controlPlane("wrong")("GET", "/api/v1/users/me")).resolves.toMatchObject({ status: 401 });
    const [url, init] = (fetchMock as jest.Mock).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://cognee.test/api/v1/permissions/tenants?tenant_name=x");
    expect((init.headers as Record<string, string>)["X-Api-Key"]).toBe(PRINCIPAL);
    global.fetch = jest.fn(async () => {
      throw new Error("ECONNREFUSED");
    }) as unknown as typeof fetch;
    await expect(client.controlPlane(PRINCIPAL)("GET", "/api/v1/users/me")).resolves.toMatchObject({ status: 0 });
  });

  it("indexes a repository into the canonical dataset by UUID", async () => {
    // The ingestion path uses the long-request transport, which bypasses
    // global.fetch: drive it against the in-process mock server instead.
    const { MockCognee } = await import("../../test-utils/mockCognee");
    const mock = await MockCognee.start();
    try {
      mock.setResponse("POST", "/remember", { dataset_id: "ds-1" });
      const client = new CogneeHttpClient(mock.url, AGENT_KEY);
      await client.indexRepository({ datasetName: "code", datasetId: "ds-1", repository: "https://x/y.git", runInBackground: true });
      const call = mock.assertCalled("POST", "/remember");
      expect(call.body).toMatch(/name="datasetId"\r\n\r\nds-1/);
      expect(call.body).toMatch(/name="datasetName"\r\n\r\ncode/);
    } finally {
      await mock.close();
    }
  });
});
