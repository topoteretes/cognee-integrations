import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { RecallBreaker, isBreakerError } from "../src/breaker";

describe("isBreakerError", () => {
  it("trips on 5xx", () => {
    expect(isBreakerError(new Error("Cognee request failed (500): boom"))).toBe(true);
    expect(isBreakerError(new Error("Cognee request failed (503): busy"))).toBe(true);
  });

  it("does not trip on 4xx", () => {
    expect(isBreakerError(new Error("Cognee request failed (403): denied"))).toBe(false);
    expect(isBreakerError(new Error("Cognee request failed (404): missing"))).toBe(false);
    expect(isBreakerError(new Error("Cognee request failed (401): unauthorized"))).toBe(false);
  });

  it("trips on network errors / timeouts (no HTTP status)", () => {
    expect(isBreakerError(new Error("fetch failed"))).toBe(true);
    expect(isBreakerError(new DOMException("The operation was aborted.", "AbortError"))).toBe(true);
  });
});

describe("RecallBreaker", () => {
  let dir: string;
  let path: string;

  beforeEach(async () => {
    dir = await mkdtemp(join(tmpdir(), "cognee-breaker-test-"));
    path = join(dir, "recall-breaker.json");
  });

  afterEach(async () => {
    await rm(dir, { recursive: true, force: true });
  });

  it("stays closed below the threshold", async () => {
    const breaker = new RecallBreaker(3, 60_000, path);
    await breaker.recordFailure("err 1");
    await breaker.recordFailure("err 2");
    expect(await breaker.openForSeconds()).toBe(0);
  });

  it("opens at the threshold and reports remaining cooldown", async () => {
    const breaker = new RecallBreaker(3, 60_000, path);
    for (let i = 0; i < 3; i++) await breaker.recordFailure(`err ${i}`);
    const remaining = await breaker.openForSeconds();
    expect(remaining).toBeGreaterThan(50);
    expect(remaining).toBeLessThanOrEqual(60);
  });

  it("uses the shared claude/codex state shape (failures, cooldown_until, last_error)", async () => {
    const breaker = new RecallBreaker(1, 60_000, path);
    await breaker.recordFailure("some error");
    const state = JSON.parse(await readFile(path, "utf-8"));
    expect(typeof state.failures).toBe("number");
    expect(typeof state.cooldown_until).toBe("number");
    expect(state.cooldown_until).toBeGreaterThan(Date.now() / 1000); // epoch SECONDS
    expect(state.cooldown_until).toBeLessThan(Date.now() / 1000 + 120);
    expect(state.last_error).toBe("some error");
  });

  it("success resets the breaker", async () => {
    const breaker = new RecallBreaker(2, 60_000, path);
    await breaker.recordFailure("err 1");
    await breaker.recordFailure("err 2");
    expect(await breaker.openForSeconds()).toBeGreaterThan(0);

    await breaker.recordSuccess();
    expect(await breaker.openForSeconds()).toBe(0);
    const state = JSON.parse(await readFile(path, "utf-8"));
    expect(state.failures).toBe(0);
  });

  it("re-trips immediately on a failed half-open probe", async () => {
    const breaker = new RecallBreaker(2, 100, path); // 100ms cooldown for the test
    await breaker.recordFailure("err 1");
    await breaker.recordFailure("err 2");
    await new Promise((r) => setTimeout(r, 150)); // cooldown elapses → half-open
    expect(await breaker.openForSeconds()).toBe(0);

    await breaker.recordFailure("probe failed"); // failures were not reset on trip
    expect(await breaker.openForSeconds()).toBeGreaterThan(0);
  });

  it("missing state file means closed", async () => {
    const breaker = new RecallBreaker(5, 60_000, join(dir, "does-not-exist.json"));
    expect(await breaker.openForSeconds()).toBe(0);
  });
});
