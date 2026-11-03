/**
 * Plugin identity (SDK-646): a dedicated "openclaw" cognee agent.
 *
 * `pluginIdentity` picks the policy. `auto` (default) and `false` keep today's
 * behaviour — every call runs as the principal — while `true` provisions the
 * plugin's own agent through the server's provision route, caches this
 * machine's key, and runs as that agent. The provision call carries the
 * plugin's label and connection type, so the server needs no hardcoded plugin
 * list, and every registration names the plugin in its metadata.
 */

import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CogneeHttpClient, CogneeRequestError } from "../../src/client";
import { resolveConfig } from "../../src/config";
import {
  CONNECTION_TYPE,
  PLUGIN_KEY,
  PLUGIN_LABEL,
  PluginIdentityError,
  ensurePluginIdentity,
  fingerprint,
  parsePluginIdentityMode,
  registerAgentUnderIdentity,
  type IdentitySession,
  type ProvisionResult,
} from "../../src/plugin-identity";

const BASE_URL = "http://127.0.0.1:8011";
const PRINCIPAL = "principal-key";
const AGENT_KEY = "agentkey-1";
const AGENT_ID = "agent-uuid-1";
const PROVISIONED: ProvisionResult = { apiKey: AGENT_KEY, agentId: AGENT_ID, keyId: "k1", created: true };

class FakeClient {
  keys: string[] = [];
  additive = true;
  provisionOutcomes: (ProvisionResult | Error)[] = [];
  provisionCalls: { pluginKey: string; description: { label: string; connectionType: string }; asKey: string }[] = [];
  registerOutcomes: ({ ok: boolean; connectionId?: string } | Error)[] = [];
  registerCalls: { params: unknown; asKey: string }[] = [];
  probeCalls = 0;

  constructor(private current = PRINCIPAL) {}

  setApiKey(key: string): void {
    this.current = key;
    this.keys.push(key);
  }
  async additivePluginKeysSupported(): Promise<boolean> {
    this.probeCalls += 1;
    return this.additive;
  }
  async provisionPlugin(pluginKey: string, description: { label: string; connectionType: string }): Promise<ProvisionResult> {
    this.provisionCalls.push({ pluginKey, description, asKey: this.current });
    const outcome = this.provisionOutcomes.length > 1 ? this.provisionOutcomes.shift()! : (this.provisionOutcomes[0] ?? PROVISIONED);
    if (outcome instanceof Error) throw outcome;
    return outcome;
  }
  async registerAgent(params: unknown): Promise<{ ok: boolean; connectionId?: string }> {
    this.registerCalls.push({ params, asKey: this.current });
    const outcome = this.registerOutcomes.length > 1 ? this.registerOutcomes.shift()! : (this.registerOutcomes[0] ?? { ok: true, connectionId: "c1" });
    if (outcome instanceof Error) throw outcome;
    return outcome;
  }
}

let dir: string;
let cachePath: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "cognee-openclaw-identity-"));
  cachePath = join(dir, PLUGIN_KEY, "agent_key.json");
});
afterEach(() => rmSync(dir, { recursive: true, force: true }));

const readCache = () => JSON.parse(readFileSync(cachePath, "utf-8")) as Record<string, unknown>;
const seedCache = (extra: Record<string, unknown> = {}) => {
  mkdirSync(join(dir, PLUGIN_KEY), { recursive: true });
  writeFileSync(
    cachePath,
    JSON.stringify({
      base_url: BASE_URL,
      api_key: AGENT_KEY,
      agent_id: AGENT_ID,
      key_id: "k1",
      plugin_key: PLUGIN_KEY,
      principal_fingerprint: fingerprint(PRINCIPAL),
      ...extra,
    }),
  );
};
const opts = (mode: "auto" | "enabled" | "disabled", principalKey = PRINCIPAL) => ({ baseUrl: BASE_URL, principalKey, mode, cachePath });

describe("parsePluginIdentityMode", () => {
  it("defaults to auto", () => {
    expect(parsePluginIdentityMode(undefined)).toBe("auto");
    expect(parsePluginIdentityMode("")).toBe("auto");
    expect(parsePluginIdentityMode("AUTO")).toBe("auto");
  });
  it.each(["true", "1", "yes", "ON", true])("%s enables", (v) => expect(parsePluginIdentityMode(v)).toBe("enabled"));
  it.each(["false", "0", "no", "off", false])("%s disables", (v) => expect(parsePluginIdentityMode(v)).toBe("disabled"));
  it("rejects garbage", () => expect(() => parsePluginIdentityMode("sometimes")).toThrow(/auto, true, or false/));
});

describe("resolveConfig pluginIdentity", () => {
  const env = process.env.COGNEE_PLUGIN_IDENTITY;
  afterEach(() => {
    if (env === undefined) delete process.env.COGNEE_PLUGIN_IDENTITY;
    else process.env.COGNEE_PLUGIN_IDENTITY = env;
  });

  it("defaults to auto", () => {
    delete process.env.COGNEE_PLUGIN_IDENTITY;
    expect(resolveConfig({}).pluginIdentity).toBe("auto");
  });
  it("reads the config value, booleans included", () => {
    expect(resolveConfig({ pluginIdentity: "true" }).pluginIdentity).toBe("true");
    expect(resolveConfig({ pluginIdentity: true }).pluginIdentity).toBe("true");
    expect(resolveConfig({ pluginIdentity: false }).pluginIdentity).toBe("false");
  });
  it("falls back to COGNEE_PLUGIN_IDENTITY and lets config win", () => {
    process.env.COGNEE_PLUGIN_IDENTITY = "true";
    expect(resolveConfig({}).pluginIdentity).toBe("true");
    expect(resolveConfig({ pluginIdentity: "false" }).pluginIdentity).toBe("false");
  });
  it("fails loudly on a typo", () => {
    expect(() => resolveConfig({ pluginIdentity: "yes please" as never })).toThrow(/auto, true, or false/);
  });
});

describe("CogneeHttpClient identity routes", () => {
  const originalFetch = global.fetch;
  afterEach(() => {
    global.fetch = originalFetch;
  });
  function respond(status: number, body: unknown) {
    return jest.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })) as unknown as typeof fetch;
  }

  it("provisions as the principal and describes the plugin", async () => {
    const fetchMock = respond(201, { pluginKey: "openclaw", agentId: AGENT_ID, apiKey: AGENT_KEY, keyId: "k1", created: true });
    global.fetch = fetchMock;
    const client = new CogneeHttpClient("http://cognee.test", PRINCIPAL);
    const result = await client.provisionPlugin(PLUGIN_KEY, { label: PLUGIN_LABEL, connectionType: CONNECTION_TYPE });
    expect(result).toEqual(PROVISIONED);
    const [url, init] = (fetchMock as jest.Mock).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://cognee.test/api/v1/integrations/plugins/openclaw/provision");
    expect((init.headers as Record<string, string>)["X-Api-Key"]).toBe(PRINCIPAL);
    expect(JSON.parse(String(init.body))).toEqual({ label: "OpenClaw", connection_type: "openclaw" });
  });

  it("accepts snake_case response fields", async () => {
    global.fetch = respond(201, { api_key: AGENT_KEY, agent_id: AGENT_ID, key_id: "k1", created: false });
    const client = new CogneeHttpClient("http://cognee.test", PRINCIPAL);
    await expect(client.provisionPlugin(PLUGIN_KEY, { label: PLUGIN_LABEL, connectionType: CONNECTION_TYPE })).resolves.toEqual({ ...PROVISIONED, created: false });
  });

  it("surfaces the key cap as a 409 request error", async () => {
    global.fetch = respond(409, { detail: "full" });
    const client = new CogneeHttpClient("http://cognee.test", PRINCIPAL);
    await expect(client.provisionPlugin(PLUGIN_KEY, { label: PLUGIN_LABEL, connectionType: CONNECTION_TYPE })).rejects.toMatchObject({ status: 409 });
  });

  it("reads the additive-keys marker from the OpenAPI document", async () => {
    const client = new CogneeHttpClient("http://cognee.test", PRINCIPAL);
    global.fetch = respond(200, { paths: { "/api/v1/integrations/plugins/{plugin_key}/provision": { post: { "x-cognee-additive-plugin-keys": true } } } });
    await expect(client.additivePluginKeysSupported()).resolves.toBe(true);
    global.fetch = respond(200, { paths: { "/api/v1/integrations/plugins/{plugin_key}/provision": { post: {} } } });
    await expect(client.additivePluginKeysSupported()).resolves.toBe(false);
    global.fetch = respond(500, { detail: "boom" });
    await expect(client.additivePluginKeysSupported()).resolves.toBe(false);
  });

  it("names the plugin in the registration metadata", async () => {
    const fetchMock = respond(201, { id: "c1" });
    global.fetch = fetchMock;
    const client = new CogneeHttpClient("http://cognee.test", PRINCIPAL);
    await client.registerAgent({ agentSessionName: "s1" });
    const [, init] = (fetchMock as jest.Mock).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(body.type).toBe("openclaw");
    expect(body.metadata).toEqual({ plugin_key: "openclaw" });
  });
});

describe("ensurePluginIdentity", () => {
  it("auto and false stay on the principal without touching the server", async () => {
    for (const mode of ["auto", "disabled"] as const) {
      const client = new FakeClient();
      await expect(ensurePluginIdentity(client, opts(mode))).resolves.toBeUndefined();
      expect(client.probeCalls).toBe(0);
      expect(client.provisionCalls).toEqual([]);
    }
  });

  it("false ignores a cached identity", async () => {
    seedCache();
    await expect(ensurePluginIdentity(new FakeClient(), opts("disabled"))).resolves.toBeUndefined();
  });

  it("true provisions as the principal, describes the plugin, and caches the key", async () => {
    const client = new FakeClient();
    const identity = await ensurePluginIdentity(client, opts("enabled"));
    expect(identity).toEqual({ apiKey: AGENT_KEY, agentId: AGENT_ID, keyId: "k1", source: "provisioned" });
    expect(client.provisionCalls).toEqual([
      { pluginKey: PLUGIN_KEY, description: { label: PLUGIN_LABEL, connectionType: CONNECTION_TYPE }, asKey: PRINCIPAL },
    ]);
    const record = readCache();
    expect(record).toMatchObject({ base_url: BASE_URL, api_key: AGENT_KEY, agent_id: AGENT_ID, key_id: "k1", plugin_key: PLUGIN_KEY, principal_fingerprint: fingerprint(PRINCIPAL) });
    expect(statSync(cachePath).mode & 0o777).toBe(0o600);
  });

  it("reuses the cached identity without provisioning", async () => {
    seedCache();
    const client = new FakeClient();
    const identity = await ensurePluginIdentity(client, opts("enabled"));
    expect(identity).toEqual({ apiKey: AGENT_KEY, agentId: AGENT_ID, keyId: "k1", source: "cached" });
    expect(client.probeCalls).toBe(0);
    expect(client.provisionCalls).toEqual([]);
  });

  it("scopes the cache to the server url", async () => {
    seedCache({ base_url: "http://other:9999" });
    const client = new FakeClient();
    await ensurePluginIdentity(client, opts("enabled"));
    expect(client.provisionCalls).toHaveLength(1);
  });

  it("refuses an identity provisioned by another principal", async () => {
    seedCache({ principal_fingerprint: fingerprint("someone-else") });
    await expect(ensurePluginIdentity(new FakeClient(), opts("enabled"))).rejects.toThrow(/another principal/);
  });

  it("never provisions against a server that rotates keys", async () => {
    const client = new FakeClient();
    client.additive = false;
    await expect(ensurePluginIdentity(client, opts("enabled"))).rejects.toThrow(/adds plugin keys per machine/);
    expect(client.provisionCalls).toEqual([]);
  });

  it("names the keys command when the agent is full", async () => {
    const client = new FakeClient();
    client.provisionOutcomes = [new CogneeRequestError(409, "full")];
    await expect(ensurePluginIdentity(client, opts("enabled"))).rejects.toThrow(/openclaw cognee keys/);
  });

  it("treats a missing route as unsupported", async () => {
    const client = new FakeClient();
    client.provisionOutcomes = [new CogneeRequestError(404, "no")];
    await expect(ensurePluginIdentity(client, opts("enabled"))).rejects.toThrow(/unsupported/);
  });

  it("rejects the principal handed back as the agent key, and a missing agent id", async () => {
    const client = new FakeClient();
    client.provisionOutcomes = [{ ...PROVISIONED, apiKey: PRINCIPAL }];
    await expect(ensurePluginIdentity(client, opts("enabled"))).rejects.toThrow(/agent_key_equals_principal/);
    client.provisionOutcomes = [{ ...PROVISIONED, agentId: "" }];
    await expect(ensurePluginIdentity(client, opts("enabled"))).rejects.toThrow(/missing_agent_id/);
  });

  it("needs a principal key", async () => {
    await expect(ensurePluginIdentity(new FakeClient(), opts("enabled", ""))).rejects.toThrow(/principal API key/);
  });

  it("does not reuse a blocked key", async () => {
    seedCache({ api_key: "agentkey-dead", blocked: true });
    const client = new FakeClient();
    const identity = await ensurePluginIdentity(client, opts("enabled"));
    expect(identity?.apiKey).toBe(AGENT_KEY);
    expect(client.provisionCalls).toHaveLength(1);
  });
});

describe("registerAgentUnderIdentity", () => {
  const session = (identity?: IdentitySession["identity"]): IdentitySession => ({ ...opts("enabled"), identity });

  it("is a plain registration without an identity", async () => {
    const client = new FakeClient();
    client.registerOutcomes = [new CogneeRequestError(401, "nope")];
    await expect(registerAgentUnderIdentity(client, { agentSessionName: "s1" }, session())).rejects.toBeInstanceOf(CogneeRequestError);
  });

  it("blocks a rejected key, replaces it once, and retries as the new agent", async () => {
    seedCache({ api_key: "agentkey-dead" });
    const client = new FakeClient("agentkey-dead");
    client.registerOutcomes = [new CogneeRequestError(401, "revoked"), { ok: true, connectionId: "c2" }];
    const s = session({ apiKey: "agentkey-dead", agentId: AGENT_ID, keyId: "k0", source: "cached" });
    await expect(registerAgentUnderIdentity(client, { agentSessionName: "s1" }, s)).resolves.toEqual({ ok: true, connectionId: "c2" });
    expect(client.registerCalls.map((c) => c.asKey)).toEqual(["agentkey-dead", AGENT_KEY]);
    expect(client.provisionCalls[0].asKey).toBe(PRINCIPAL);
    expect(s.identity?.apiKey).toBe(AGENT_KEY);
    expect(readCache()).toMatchObject({ api_key: AGENT_KEY });
    expect(readCache().blocked).toBeUndefined();
  });

  it("a second rejection is an error, not a fallback", async () => {
    seedCache({ api_key: "agentkey-dead" });
    const client = new FakeClient("agentkey-dead");
    client.registerOutcomes = [new CogneeRequestError(401, "x"), new CogneeRequestError(401, "x")];
    const s = session({ apiKey: "agentkey-dead", agentId: AGENT_ID, keyId: "k0", source: "cached" });
    await expect(registerAgentUnderIdentity(client, { agentSessionName: "s1" }, s)).rejects.toThrow(PluginIdentityError);
    expect(readCache().blocked).toBe(true);
  });

  it("a 403 keeps the key and raises", async () => {
    seedCache();
    const client = new FakeClient(AGENT_KEY);
    client.registerOutcomes = [new CogneeRequestError(403, "forbidden")];
    const s = session({ apiKey: AGENT_KEY, agentId: AGENT_ID, keyId: "k1", source: "cached" });
    await expect(registerAgentUnderIdentity(client, { agentSessionName: "s1" }, s)).rejects.toThrow(/403/);
    expect(readCache().blocked).toBeUndefined();
    expect(client.provisionCalls).toEqual([]);
  });
});
