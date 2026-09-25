import { CogneeApiError, isBreakerError, isRetryable } from "../../src/errors.js";

describe("CogneeApiError.fromResponse", () => {
  it("extracts the message from a `detail` body (FastAPI-style envelope)", () => {
    const err = CogneeApiError.fromResponse(422, { detail: "invalid payload" }, { url: "/api/v1/recall", method: "POST" });
    expect(err).toBeInstanceOf(CogneeApiError);
    expect(err).toBeInstanceOf(Error);
    expect(err.name).toBe("CogneeApiError");
    expect(err.message).toBe("invalid payload");
    expect(err.status).toBe(422);
    expect(err.url).toBe("/api/v1/recall");
    expect(err.method).toBe("POST");
    expect(err.body).toEqual({ detail: "invalid payload" });
  });

  it("extracts the message from an `error` body (ad-hoc route handler envelope)", () => {
    const err = CogneeApiError.fromResponse(422, { error: "validation failed" });
    expect(err.message).toBe("validation failed");
  });

  it("extracts the message from a bare `message` body (e.g. /health failure)", () => {
    const err = CogneeApiError.fromResponse(503, { message: "not ready" });
    expect(err.message).toBe("not ready");
  });

  it("falls back to a generic message when the body has none of the known keys", () => {
    const err = CogneeApiError.fromResponse(500, { unexpected: "shape" });
    expect(err.message).toBe("cognee API responded with status 500");
  });

  it("falls back to a generic message when the body is a non-string or empty", () => {
    expect(CogneeApiError.fromResponse(404, undefined).message).toBe("cognee API responded with status 404");
    expect(CogneeApiError.fromResponse(404, null).message).toBe("cognee API responded with status 404");
    expect(CogneeApiError.fromResponse(404, "").message).toBe("cognee API responded with status 404");
    expect(CogneeApiError.fromResponse(404, "   ").message).toBe("cognee API responded with status 404");
  });

  it("uses a plain string body verbatim as the message", () => {
    const err = CogneeApiError.fromResponse(409, "conflict: dataset locked");
    expect(err.message).toBe("conflict: dataset locked");
  });

  it("prefers `detail` over `error` over `message` when more than one key is present", () => {
    const err = CogneeApiError.fromResponse(422, { detail: "from detail", error: "from error", message: "from message" });
    expect(err.message).toBe("from detail");
  });
});

describe("CogneeApiError.fromNetworkError", () => {
  it("uses status 0 and the underlying Error's message", () => {
    const err = CogneeApiError.fromNetworkError(new Error("ECONNREFUSED"), { url: "http://localhost:8000/health" });
    expect(err.status).toBe(0);
    expect(err.message).toBe("cognee request failed: ECONNREFUSED");
    expect(err.url).toBe("http://localhost:8000/health");
  });

  it("stringifies a non-Error cause", () => {
    const err = CogneeApiError.fromNetworkError("boom");
    expect(err.status).toBe(0);
    expect(err.message).toBe("cognee request failed: boom");
  });
});

describe("CogneeApiError.fromTimeout", () => {
  it("uses status 0 and reports the configured timeout", () => {
    const err = CogneeApiError.fromTimeout(2500, { method: "POST", url: "/api/v1/recall" });
    expect(err.status).toBe(0);
    expect(err.message).toBe("cognee request timed out after 2500ms");
    expect(err.method).toBe("POST");
  });
});

describe("isRetryable — status -> class matrix", () => {
  it.each([402, 403, 404, 409, 422])("status %d is NOT retryable (cognee's stable 'no')", (status) => {
    expect(isRetryable(status)).toBe(false);
  });

  it.each([500, 502, 503, 504, 599])("5xx status %d IS retryable", (status) => {
    expect(isRetryable(status)).toBe(true);
  });

  it("429 (rate limited) is retryable", () => {
    expect(isRetryable(429)).toBe(true);
  });

  it("2xx/3xx are not retryable (never reached from an error path, but the table has no opinion in the affirmative)", () => {
    expect(isRetryable(200)).toBe(false);
    expect(isRetryable(304)).toBe(false);
  });

  it("400 (generic bad request, not in the explicit matrix) is not retryable", () => {
    expect(isRetryable(400)).toBe(false);
  });

  it("401 (unauthorized, not in the explicit matrix) is not retryable", () => {
    expect(isRetryable(401)).toBe(false);
  });

  it("0 (network error / timeout, no response) has no opinion from this table", () => {
    expect(isRetryable(0)).toBe(false);
  });

  it("600 (out of the 5xx band) is not retryable", () => {
    expect(isRetryable(600)).toBe(false);
  });
});

describe("isBreakerError — status -> breaker-eligibility matrix", () => {
  it.each([402, 403, 404, 409, 422])(
    "status %d does NOT count against the breaker (a deterministic 4xx, not evidence cognee is down)",
    (status) => {
      expect(isBreakerError(status)).toBe(false);
    },
  );

  it("400 / 401 / 429 also do not count against the breaker", () => {
    expect(isBreakerError(400)).toBe(false);
    expect(isBreakerError(401)).toBe(false);
    expect(isBreakerError(429)).toBe(false);
  });

  it.each([500, 502, 503, 504, 599])("5xx status %d DOES count against the breaker", (status) => {
    expect(isBreakerError(status)).toBe(true);
  });

  it("0 (network error / timeout, no response) counts against the breaker", () => {
    expect(isBreakerError(0)).toBe(true);
  });

  it("2xx/3xx are not breaker-eligible (never reached from an error path)", () => {
    expect(isBreakerError(200)).toBe(false);
    expect(isBreakerError(304)).toBe(false);
  });

  it("600 (out of the 5xx band) does not count against the breaker", () => {
    expect(isBreakerError(600)).toBe(false);
  });
});
