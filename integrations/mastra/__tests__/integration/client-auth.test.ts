/**
 * `CogneeClient` auth: X-Api-Key header, JWT login + token reuse, the
 * 401-triggers-one-relogin-and-retry path (JWT mode only), apiKey winning
 * over auth, and the no-credentials error shape.
 *
 * Real `node:http` mock server throughout — auth headers and the login form
 * body are asserted on the wire, not on fetch call arguments.
 */

import { jest } from "@jest/globals";
import { CogneeClient } from "../../src/client.js";
import { startMockCognee, MOCK_API_KEY, MOCK_JWT_TOKEN, type MockCognee } from "../test-utils/mock-cognee.js";
import { LIST_DATASETS_RESPONSE } from "../test-utils/fixtures.js";

let mock: MockCognee;

afterEach(async () => {
  if (mock) await mock.close();
});

// ---------------------------------------------------------------------------
// X-Api-Key
// ---------------------------------------------------------------------------

describe("X-Api-Key header", () => {
  it("sends X-Api-Key when apiKey is configured, and never also a Bearer token", async () => {
    mock = await startMockCognee({ requireAuth: "apiKey" });
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    await client.listDatasets();

    const req = mock.requests.find((r) => r.path === "/api/v1/datasets");
    expect(req).toBeDefined();
    expect(req!.headers["x-api-key"]).toBe(MOCK_API_KEY);
    // X-Api-Key alone, never also a stale Bearer token — asserted on the
    // wire, not just on client internals.
    expect(req!.headers["authorization"]).toBeUndefined();
  });

  it("never calls the login route when an apiKey is configured", async () => {
    mock = await startMockCognee({ requireAuth: "apiKey" });
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    await client.listDatasets();
    await client.ensureDataset("some-dataset");

    expect(mock.requests.some((r) => r.path === "/api/v1/auth/login")).toBe(false);
  });

  it("apiKey wins over auth when both are configured", async () => {
    mock = await startMockCognee({ requireAuth: "apiKey" });
    const client = new CogneeClient({
      baseUrl: mock.url,
      apiKey: MOCK_API_KEY,
      // Wrong on purpose — if the client ever fell back to these, the
      // request would either 401 (wrong route, no key) or hit the login
      // route, either of which this test would catch.
      auth: { email: "someone@example.com", password: "this-should-never-be-used" },
    });

    const result = await client.listDatasets();

    expect(result).toEqual(LIST_DATASETS_RESPONSE);
    expect(mock.requests.some((r) => r.path === "/api/v1/auth/login")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// JWT login flow
// ---------------------------------------------------------------------------

describe("JWT login flow", () => {
  it("logs in once (form-encoded username/password) and reuses the cached token across multiple requests", async () => {
    mock = await startMockCognee({ requireAuth: "jwt" });
    const client = new CogneeClient({ baseUrl: mock.url, auth: { email: "user@example.com", password: "s3cret" } });

    const first = await client.listDatasets();
    const second = await client.listDatasets();

    expect(first).toEqual(LIST_DATASETS_RESPONSE);
    expect(second).toEqual(LIST_DATASETS_RESPONSE);

    const loginRequests = mock.requests.filter((r) => r.path === "/api/v1/auth/login");
    expect(loginRequests.length).toBe(1); // token cached in memory, never re-fetched for a second call.

    const login = loginRequests[0]!;
    expect(login.headers["content-type"]).toContain("application/x-www-form-urlencoded");
    // OAuth2PasswordRequestForm field names are `username`/`password`, not `email`.
    expect(login.body).toContain("username=user%40example.com");
    expect(login.body).toContain("password=s3cret");

    const datasetRequests = mock.requests.filter((r) => r.path === "/api/v1/datasets");
    expect(datasetRequests.length).toBe(2);
    for (const req of datasetRequests) {
      expect(req.headers["authorization"]).toBe(`Bearer ${MOCK_JWT_TOKEN}`);
      expect(req.headers["x-api-key"]).toBeUndefined();
    }
  });

  it("throws a clear, non-retryable error when neither apiKey nor auth credentials are configured", async () => {
    mock = await startMockCognee();
    const client = new CogneeClient({ baseUrl: mock.url });

    await expect(client.listDatasets()).rejects.toThrow(/no credentials configured/i);
    // The no-credentials error throws only on the first authenticated
    // request, never at construction time, and never reaches the network
    // (a status-0 CogneeApiError, not a route hit).
    expect(mock.requests.length).toBe(0);
  });

  it("re-logs in exactly once and retries the same request after a 401 (JWT mode only)", async () => {
    let datasetCalls = 0;
    mock = await startMockCognee({
      requireAuth: false, // full control over the 401 sequence via the override below instead.
      routes: {
        "GET /api/v1/datasets": () => {
          datasetCalls += 1;
          // Simulate an expired/rotated token on the first call only.
          if (datasetCalls === 1) return { status: 401, body: { detail: "token expired" } };
          return { status: 200, body: LIST_DATASETS_RESPONSE };
        },
      },
    });
    const client = new CogneeClient({ baseUrl: mock.url, auth: { email: "user@example.com", password: "s3cret" } });

    const result = await client.listDatasets();

    expect(result).toEqual(LIST_DATASETS_RESPONSE);
    expect(datasetCalls).toBe(2); // original 401 + the one silent retry.
    const loginRequests = mock.requests.filter((r) => r.path === "/api/v1/auth/login");
    expect(loginRequests.length).toBe(2); // initial login + the re-login the 401 triggers.
  });

  it("does NOT re-log-in on a 401 when an apiKey is configured (a bad key is not a stale token)", async () => {
    mock = await startMockCognee({
      requireAuth: false,
      routes: {
        "GET /api/v1/datasets": () => ({ status: 401, body: { detail: "invalid api key" } }),
      },
    });
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: "bad-key", retries: 0 });

    await expect(client.listDatasets()).rejects.toMatchObject({ status: 401 });
    expect(mock.requests.some((r) => r.path === "/api/v1/auth/login")).toBe(false);
    expect(mock.requests.filter((r) => r.path === "/api/v1/datasets").length).toBe(1);
  });

  it("never logs the password or the returned access token on the JWT login path, even with debug: true", async () => {
    mock = await startMockCognee({ requireAuth: "jwt" });
    const password = "s3cret-password-must-not-be-logged";
    const logSpy = jest.spyOn(console, "debug").mockImplementation(() => {});
    try {
      const client = new CogneeClient({
        baseUrl: mock.url,
        auth: { email: "user@example.com", password },
        debug: true,
      });

      await client.listDatasets();

      // `debugLog` (client.ts) is gated by `debug: true` and fires for both
      // the login and dataset requests — this proves it ran, not just that
      // logging is silent by default, while asserting neither the password
      // nor the returned token reaches a log line.
      expect(logSpy).toHaveBeenCalled();
      const loggedText = logSpy.mock.calls.map((call) => call.join(" ")).join("\n");
      expect(loggedText).not.toContain(password);
      expect(loggedText).not.toContain(MOCK_JWT_TOKEN);
    } finally {
      logSpy.mockRestore();
    }
  });
});
