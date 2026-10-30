/**
 * Plugin agent keys (cognee#6113): the client's listing, revocation and the
 * "most recently used first" order the `cognee keys` command shows.
 */

import { CogneeHttpClient } from "../../src/client";
import { sortPluginKeysByLastUse } from "../../src/plugin-keys";

const key = (id: string, createdAt: string, lastUsedAt: string) => ({ plugin: "claude-code", id, label: "", createdAt, lastUsedAt });

describe("sortPluginKeysByLastUse", () => {
  it("orders by last use, newest first, with never-used keys last by creation", () => {
    const sorted = sortPluginKeysByLastUse([
      key("unused-old", "2026-09-01T00:00:00Z", ""),
      key("used-sep", "2026-08-01T00:00:00Z", "2026-09-20T00:00:00Z"),
      key("unused-new", "2026-10-19T00:00:00Z", ""),
      key("used-oct", "2026-07-01T00:00:00Z", "2026-10-20T00:00:00Z"),
    ]);
    expect(sorted.map((k) => k.id)).toEqual(["used-oct", "used-sep", "unused-new", "unused-old"]);
  });
});

describe("CogneeHttpClient plugin keys", () => {
  const originalFetch = global.fetch;
  afterEach(() => {
    global.fetch = originalFetch;
  });

  function respond(status: number, body: unknown) {
    return jest.fn(async () =>
      new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
    ) as unknown as typeof fetch;
  }

  it("lists a plugin agent's keys as the principal and normalises the fields", async () => {
    const fetchMock = respond(200, {
      keys: [{ id: "k1", label: "aaaa****", created_at: "2026-10-01T00:00:00Z", last_used_at: null }],
    });
    global.fetch = fetchMock;
    const client = new CogneeHttpClient("http://cognee.test", "principal-key");
    const keys = await client.listPluginKeys("claude-code");
    expect(keys).toEqual([{ plugin: "claude-code", id: "k1", label: "aaaa****", createdAt: "2026-10-01T00:00:00Z", lastUsedAt: "" }]);
    const [url, init] = (fetchMock as jest.Mock).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://cognee.test/api/v1/integrations/plugins/claude-code/keys");
    expect((init.headers as Record<string, string>)["X-Api-Key"]).toBe("principal-key");
  });

  it("treats a server without key routes as an empty listing", async () => {
    global.fetch = respond(404, { detail: "Not Found" });
    const client = new CogneeHttpClient("http://cognee.test", "principal-key");
    await expect(client.listPluginKeys("claude-code")).resolves.toEqual([]);
  });

  it("revokes one key, reporting whether it existed", async () => {
    global.fetch = respond(200, { revoked: true });
    const client = new CogneeHttpClient("http://cognee.test", "principal-key");
    await expect(client.revokePluginKey("codex", "k1")).resolves.toEqual({ found: true });
    global.fetch = respond(404, { detail: "API key not found" });
    await expect(client.revokePluginKey("codex", "k1")).resolves.toEqual({ found: false });
  });

  it("uses the cloud paths in cloud mode", async () => {
    const fetchMock = respond(200, { keys: [] });
    global.fetch = fetchMock;
    const client = new CogneeHttpClient("https://api.cognee.cloud/api/v1", "tenant-key", undefined, undefined, 1000, 1000, "cloud");
    await client.listPluginKeys("claude-code");
    expect(((fetchMock as jest.Mock).mock.calls[0] as [string])[0]).toBe("https://api.cognee.cloud/api/v1/integrations/plugins/claude-code/keys");
  });
});
