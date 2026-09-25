/**
 * `src/capabilities.ts` caches, per base URL, which route variant a cognee
 * server has — recall vs search, prefixed vs unprefixed auth — and the pure
 * resolve* state machines behind that cache.
 *
 * Covers the pure logic directly (no network) and, network-backed, that the
 * cache is shared across independent CogneeClient instances on one base URL.
 */

import { CogneeClient } from "../../src/client.js";
import { CogneeApiError } from "../../src/errors.js";
import {
  getCapabilities,
  isRouteMissing,
  resetCapabilities,
  resolveAuthPrefix,
  resolveRecallPath,
  REMEMBER_IMPLIES_COGNIFY,
  type AuthPrefix,
  type RecallPath,
} from "../../src/capabilities.js";
import { startMockCognee, MOCK_API_KEY, MOCK_JWT_TOKEN, type MockCognee } from "../test-utils/mock-cognee.js";
import { LOGIN_RESPONSE } from "../test-utils/fixtures.js";

let mock: MockCognee;

afterEach(async () => {
  if (mock) await mock.close();
});

// ---------------------------------------------------------------------------
// isRouteMissing
// ---------------------------------------------------------------------------

describe("isRouteMissing", () => {
  it("is true only for a CogneeApiError with status 404", () => {
    expect(isRouteMissing(new CogneeApiError("not found", { status: 404 }))).toBe(true);
    expect(isRouteMissing(new CogneeApiError("server error", { status: 500 }))).toBe(false);
    expect(isRouteMissing(new CogneeApiError("bad request", { status: 400 }))).toBe(false);
    expect(isRouteMissing(new Error("not a CogneeApiError"))).toBe(false);
    expect(isRouteMissing("not even an Error")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// resolveRecallPath — pure logic, synthetic `run`
// ---------------------------------------------------------------------------

describe("resolveRecallPath (pure logic)", () => {
  it("tries /recall first; a cache with nothing set calls the primary path directly", async () => {
    const cache = getCapabilities("test://recall-pure-1");
    const calls: RecallPath[] = [];
    const result = await resolveRecallPath(cache, async (path) => {
      calls.push(path);
      return `ok:${path}`;
    });
    expect(result).toBe("ok:/api/v1/recall");
    expect(calls).toEqual(["/api/v1/recall"]);
    expect(cache.recallPath).toBe("/api/v1/recall");
  });

  it("falls back to /search exactly once on a 404, and caches /search as the winner", async () => {
    const cache = getCapabilities("test://recall-pure-2");
    const calls: RecallPath[] = [];
    const result = await resolveRecallPath(cache, async (path) => {
      calls.push(path);
      if (path === "/api/v1/recall") throw new CogneeApiError("not found", { status: 404 });
      return `ok:${path}`;
    });
    expect(result).toBe("ok:/api/v1/search");
    expect(calls).toEqual(["/api/v1/recall", "/api/v1/search"]);
    expect(cache.recallPath).toBe("/api/v1/search");

    // A second call must go straight to /search — no repeat of the 404 probe.
    const secondResult = await resolveRecallPath(cache, async (path) => {
      calls.push(path);
      return `ok:${path}`;
    });
    expect(secondResult).toBe("ok:/api/v1/search");
    expect(calls).toEqual(["/api/v1/recall", "/api/v1/search", "/api/v1/search"]);
  });

  it("a non-404 error on /recall propagates, but STILL caches /recall as the winner (route exists)", async () => {
    const cache = getCapabilities("test://recall-pure-3");
    const calls: RecallPath[] = [];
    await expect(
      resolveRecallPath(cache, async (path) => {
        calls.push(path);
        throw new CogneeApiError("validation error", { status: 422 });
      }),
    ).rejects.toMatchObject({ status: 422 });

    expect(calls).toEqual(["/api/v1/recall"]); // never tried /search — 422 means the route exists, just rejected this call.
    expect(cache.recallPath).toBe("/api/v1/recall");
  });

  it("once cached, uses the cached path directly even if the OTHER path would also work", async () => {
    const cache = getCapabilities("test://recall-pure-4");
    cache.recallPath = "/api/v1/search";
    const calls: RecallPath[] = [];
    const result = await resolveRecallPath(cache, async (path) => {
      calls.push(path);
      return `ok:${path}`;
    });
    expect(result).toBe("ok:/api/v1/search");
    expect(calls).toEqual(["/api/v1/search"]);
  });
});

// ---------------------------------------------------------------------------
// resolveAuthPrefix — pure logic, synthetic `run`
// ---------------------------------------------------------------------------

describe("resolveAuthPrefix (pure logic)", () => {
  it("tries /api/v1/auth first and caches it on success", async () => {
    const cache = getCapabilities("test://auth-pure-1");
    const calls: AuthPrefix[] = [];
    const result = await resolveAuthPrefix(cache, async (prefix) => {
      calls.push(prefix);
      return `token:${prefix}`;
    });
    expect(result).toBe("token:/api/v1/auth");
    expect(cache.authPrefix).toBe("/api/v1/auth");
    expect(calls).toEqual(["/api/v1/auth"]);
  });

  it("falls back to the unprefixed /auth exactly once on a 404, and caches it", async () => {
    const cache = getCapabilities("test://auth-pure-2");
    const calls: AuthPrefix[] = [];
    const result = await resolveAuthPrefix(cache, async (prefix) => {
      calls.push(prefix);
      if (prefix === "/api/v1/auth") throw new CogneeApiError("not found", { status: 404 });
      return `token:${prefix}`;
    });
    expect(result).toBe("token:/auth");
    expect(calls).toEqual(["/api/v1/auth", "/auth"]);
    expect(cache.authPrefix).toBe("/auth");

    const second = await resolveAuthPrefix(cache, async (prefix) => {
      calls.push(prefix);
      return `token:${prefix}`;
    });
    expect(second).toBe("token:/auth");
    expect(calls).toEqual(["/api/v1/auth", "/auth", "/auth"]); // no repeat probe.
  });
});

// ---------------------------------------------------------------------------
// getCapabilities / resetCapabilities — the per-base-URL registry itself
// ---------------------------------------------------------------------------

describe("getCapabilities registry", () => {
  it("returns the SAME object for the same base URL (shared mutable state)", () => {
    resetCapabilities("test://registry-1");
    const a = getCapabilities("test://registry-1");
    const b = getCapabilities("test://registry-1");
    expect(a).toBe(b);
  });

  it("returns independent objects for different base URLs", () => {
    const a = getCapabilities("test://registry-2a");
    const b = getCapabilities("test://registry-2b");
    expect(a).not.toBe(b);
  });

  it("resetCapabilities(url) clears only that url's entry", () => {
    const a = getCapabilities("test://registry-3a");
    a.recallPath = "/api/v1/search";
    getCapabilities("test://registry-3b").recallPath = "/api/v1/recall";

    resetCapabilities("test://registry-3a");

    expect(getCapabilities("test://registry-3a").recallPath).toBeUndefined();
    expect(getCapabilities("test://registry-3b").recallPath).toBe("/api/v1/recall");
  });
});

// ---------------------------------------------------------------------------
// REMEMBER_IMPLIES_COGNIFY — fixed fact, not a runtime probe
// ---------------------------------------------------------------------------

describe("REMEMBER_IMPLIES_COGNIFY", () => {
  it("is true — /remember's own handler runs cognify as part of the same pipeline", () => {
    expect(REMEMBER_IMPLIES_COGNIFY).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Network-backed: auth prefix fallback against the real mock server
// ---------------------------------------------------------------------------

describe("auth prefix fallback (network)", () => {
  it("falls back to unprefixed /auth/login when /api/v1/auth/login 404s, and caches the winner", async () => {
    // The mock's built-in auth-exempt list only knows the prefixed
    // `/api/v1/auth/login`, not this test's unprefixed fallback route, so
    // `requireAuth: "jwt"` here would 401 the unprefixed login call itself.
    // The manual header check on `/api/v1/datasets` below gets the same
    // coverage without that false failure.
    mock = await startMockCognee({
      routes: {
        "POST /api/v1/auth/login": () => ({ status: 404, body: { detail: "not mounted" } }),
        "POST /auth/login": () => ({ status: 200, body: LOGIN_RESPONSE }),
        "GET /api/v1/datasets": (req) =>
          req.headers["authorization"] === `Bearer ${MOCK_JWT_TOKEN}`
            ? { status: 200, body: [] }
            : { status: 401, body: { detail: "missing or invalid bearer token" } },
      },
    });
    const client = new CogneeClient({ baseUrl: mock.url, auth: { email: "user@example.com", password: "s3cret" } });

    const result = await client.listDatasets();

    expect(result).toBeDefined();
    expect(mock.requests.filter((r) => r.path === "/api/v1/auth/login").length).toBe(1);
    expect(mock.requests.filter((r) => r.path === "/auth/login").length).toBe(1);
    const datasetReq = mock.requests.find((r) => r.path === "/api/v1/datasets")!;
    expect(datasetReq.headers["authorization"]).toBe(`Bearer ${MOCK_JWT_TOKEN}`);
    expect(getCapabilities(mock.url).authPrefix).toBe("/auth");

    // A second, independent client pointed at the same base URL must reuse
    // the cached prefix decision — never re-probing the prefixed route.
    const secondClient = new CogneeClient({ baseUrl: mock.url, auth: { email: "user2@example.com", password: "s3cret2" } });
    await secondClient.login();
    expect(mock.requests.filter((r) => r.path === "/api/v1/auth/login").length).toBe(1); // unchanged.
    expect(mock.requests.filter((r) => r.path === "/auth/login").length).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// Network-backed: the cache is shared across independent CogneeClient
// instances pointed at the same base URL
// ---------------------------------------------------------------------------

describe("capability cache is shared across CogneeClient instances (network)", () => {
  it("a second client reusing a base URL whose /recall was already found missing skips straight to /search", async () => {
    mock = await startMockCognee({
      routes: { "POST /api/v1/recall": () => ({ status: 404, body: { detail: "no such route" } }) },
    });
    const clientA = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });
    await clientA.recall({ query: "first client, triggers the probe" });

    expect(mock.requests.filter((r) => r.path === "/api/v1/recall").length).toBe(1);

    const clientB = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });
    await clientB.recall({ query: "second client, same base URL" });

    // clientB never re-probes /recall — it inherits clientA's cached decision.
    expect(mock.requests.filter((r) => r.path === "/api/v1/recall").length).toBe(1);
    expect(mock.requests.filter((r) => r.path === "/api/v1/search").length).toBe(2);
  });
});
