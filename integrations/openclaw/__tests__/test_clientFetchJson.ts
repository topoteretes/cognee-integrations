import { CogneeHttpClient } from "../src/client";

describe("CogneeHttpClient.fetchJson", () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    jest.restoreAllMocks();
  });

  it("delegates JSON requests through the shared authenticated transport", async () => {
    const fetchMock = jest.fn(async () => {
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    global.fetch = fetchMock as typeof fetch;

    const client = new CogneeHttpClient("http://localhost:8000", "test-api-key");

    await expect(client.fetchJson<{ ok: boolean }>("/api/v1/skills", { method: "GET" }))
      .resolves.toEqual({ ok: true });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/skills",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({
          Authorization: "Bearer test-api-key",
          "X-Api-Key": "test-api-key",
        }),
      }),
    );
  });
});
