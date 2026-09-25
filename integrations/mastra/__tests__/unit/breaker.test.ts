import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  CircuitBreaker,
  DEFAULT_BREAKER_COOLDOWN_MS,
  DEFAULT_BREAKER_THRESHOLD,
  defaultBreakerPath,
} from "../../src/breaker.js";

/** Deterministic, manually-advanced clock — no real sleeping in this suite. */
function fakeClock(startMs = 1_700_000_000_000) {
  let t = startMs;
  return {
    now: () => t,
    advance: (ms: number) => {
      t += ms;
    },
  };
}

describe("CircuitBreaker", () => {
  let dir: string;
  let path: string;

  beforeEach(async () => {
    dir = await mkdtemp(join(tmpdir(), "cognee-mastra-breaker-test-"));
    path = join(dir, "recall-breaker.json");
  });

  afterEach(async () => {
    await rm(dir, { recursive: true, force: true });
  });

  it("defaults to threshold 5 and cooldown 120s", () => {
    expect(DEFAULT_BREAKER_THRESHOLD).toBe(5);
    expect(DEFAULT_BREAKER_COOLDOWN_MS).toBe(120_000);
  });

  it("defaultBreakerPath() is namespaced under a .cognee-mastra directory", () => {
    // Does not assert defaultBreakerPath() resolves under jest.setup.ts's
    // sandboxed homedir: under this package's ts-jest ESM preset, its
    // `jest.mock("node:os", ...)` does not intercept a plain ESM `import
    // { homedir }`, so homedir() still returns the real OS home here. Every
    // other test passes an explicit `path` instead.
    expect(defaultBreakerPath()).toContain(".cognee-mastra");
    expect(defaultBreakerPath().endsWith("recall-breaker.json")).toBe(true);
  });

  it("stays closed below the failure threshold", async () => {
    const clock = fakeClock();
    const breaker = new CircuitBreaker({ threshold: 5, cooldownMs: 120_000, path, now: clock.now });
    for (let i = 0; i < 4; i++) await breaker.recordFailure(`err ${i}`);
    expect(await breaker.isOpen()).toBe(false);
    expect(await breaker.remainingCooldownMs()).toBe(0);
  });

  it("opens for exactly cooldownMs on the 5th consecutive failure", async () => {
    const clock = fakeClock();
    const breaker = new CircuitBreaker({ threshold: 5, cooldownMs: 120_000, path, now: clock.now });
    for (let i = 0; i < 5; i++) await breaker.recordFailure(`err ${i}`);

    expect(await breaker.isOpen()).toBe(true);
    expect(await breaker.remainingCooldownMs()).toBe(120_000);

    clock.advance(60_000); // halfway through cooldown
    expect(await breaker.isOpen()).toBe(true);
    expect(await breaker.remainingCooldownMs()).toBe(60_000);
  });

  it("half-opens (closes) the instant the fake clock crosses the cooldown boundary", async () => {
    const clock = fakeClock();
    const breaker = new CircuitBreaker({ threshold: 5, cooldownMs: 120_000, path, now: clock.now });
    for (let i = 0; i < 5; i++) await breaker.recordFailure("boom");

    clock.advance(119_999);
    expect(await breaker.isOpen()).toBe(true);

    clock.advance(1); // now exactly at cooldown_until — no longer > now()
    expect(await breaker.isOpen()).toBe(false);
    expect(await breaker.remainingCooldownMs()).toBe(0);
  });

  it("re-trips immediately on a failed half-open probe (failures are not reset on trip)", async () => {
    const clock = fakeClock();
    const breaker = new CircuitBreaker({ threshold: 2, cooldownMs: 1_000, path, now: clock.now });
    await breaker.recordFailure("err 1");
    await breaker.recordFailure("err 2");
    expect(await breaker.isOpen()).toBe(true);

    clock.advance(1_001); // cooldown elapses -> half-open
    expect(await breaker.isOpen()).toBe(false);

    await breaker.recordFailure("probe failed"); // failures accumulate past threshold again
    expect(await breaker.isOpen()).toBe(true);
    expect(await breaker.remainingCooldownMs()).toBe(1_000);
  });

  it("recordSuccess fully resets failures and cooldown", async () => {
    const clock = fakeClock();
    const breaker = new CircuitBreaker({ threshold: 3, cooldownMs: 60_000, path, now: clock.now });
    await breaker.recordFailure("e1");
    await breaker.recordFailure("e2");
    await breaker.recordFailure("e3");
    expect(await breaker.isOpen()).toBe(true);

    await breaker.recordSuccess();
    expect(await breaker.isOpen()).toBe(false);
    expect(await breaker.remainingCooldownMs()).toBe(0);

    const state = JSON.parse(await readFile(path, "utf-8"));
    expect(state.failures).toBe(0);
    expect(state.cooldownUntilMs).toBe(0);
  });

  it("missing state file means closed (never fails open on absence)", async () => {
    const clock = fakeClock();
    const breaker = new CircuitBreaker({ path: join(dir, "does-not-exist.json"), now: clock.now });
    expect(await breaker.isOpen()).toBe(false);
    expect(await breaker.remainingCooldownMs()).toBe(0);
  });

  it("corrupt (non-JSON) state file degrades to closed", async () => {
    await mkdir(dir, { recursive: true });
    await writeFile(path, "{ this is not valid json", "utf-8");
    const breaker = new CircuitBreaker({ path, now: fakeClock().now });
    expect(await breaker.isOpen()).toBe(false);
  });

  it("state file with the wrong shape (valid JSON, implausible fields) degrades to closed", async () => {
    await writeFile(path, JSON.stringify({ hello: "world" }), "utf-8");
    const breaker = new CircuitBreaker({ path, now: fakeClock().now });
    expect(await breaker.isOpen()).toBe(false);
  });

  it("state file that is valid JSON but not an object (e.g. an array) degrades to closed", async () => {
    await writeFile(path, JSON.stringify([1, 2, 3]), "utf-8");
    const breaker = new CircuitBreaker({ path, now: fakeClock().now });
    expect(await breaker.isOpen()).toBe(false);
  });

  it("a failure recorded against a corrupt state file overwrites it with valid state (self-healing)", async () => {
    await writeFile(path, "not json at all", "utf-8");
    const clock = fakeClock();
    const breaker = new CircuitBreaker({ threshold: 1, cooldownMs: 5_000, path, now: clock.now });
    await breaker.recordFailure("boom");
    const state = JSON.parse(await readFile(path, "utf-8"));
    expect(state.failures).toBe(1);
    expect(state.cooldownUntilMs).toBe(clock.now() + 5_000);
  });

  it("persists failures/cooldown across independent CircuitBreaker instances sharing a path", async () => {
    const clock = fakeClock();
    const first = new CircuitBreaker({ threshold: 3, cooldownMs: 10_000, path, now: clock.now });
    await first.recordFailure("e1");
    await first.recordFailure("e2");

    const second = new CircuitBreaker({ threshold: 3, cooldownMs: 10_000, path, now: clock.now });
    expect(await second.isOpen()).toBe(false);
    await second.recordFailure("e3"); // 3rd failure, from a different instance
    expect(await second.isOpen()).toBe(true);
    expect(await first.isOpen()).toBe(true); // same file, same verdict
  });

  it("truncates an overly long error message to 300 chars", async () => {
    const breaker = new CircuitBreaker({ threshold: 1, path, now: fakeClock().now });
    await breaker.recordFailure(new Error("x".repeat(1000)));
    const state = JSON.parse(await readFile(path, "utf-8"));
    expect(state.lastError.length).toBe(300);
  });

  it("accepts a plain string or an Error for recordFailure", async () => {
    const breaker = new CircuitBreaker({ threshold: 1, path, now: fakeClock().now });
    await breaker.recordFailure("plain string reason");
    const state = JSON.parse(await readFile(path, "utf-8"));
    expect(state.lastError).toBe("plain string reason");
  });

  it("redacts an apiKey/password-shaped substring out of a persisted error message", async () => {
    const breaker = new CircuitBreaker({ threshold: 1, path, now: fakeClock().now });
    await breaker.recordFailure(new Error("cognee API responded with status 422: invalid apiKey: sk-super-secret-value"));
    const state = JSON.parse(await readFile(path, "utf-8"));
    expect(state.lastError).not.toContain("sk-super-secret-value");
    expect(state.lastError).toContain("REDACTED");
  });

  it("redacts a password=... substring out of a persisted error message", async () => {
    const breaker = new CircuitBreaker({ threshold: 1, path, now: fakeClock().now });
    await breaker.recordFailure(new Error("login rejected: password=hunter2-do-not-log-me"));
    const state = JSON.parse(await readFile(path, "utf-8"));
    expect(state.lastError).not.toContain("hunter2-do-not-log-me");
    expect(state.lastError).toContain("REDACTED");
  });

  it("uses default threshold/cooldown when not overridden, with a fake clock", async () => {
    const clock = fakeClock();
    const breaker = new CircuitBreaker({ path, now: clock.now }); // no threshold/cooldownMs given
    for (let i = 0; i < DEFAULT_BREAKER_THRESHOLD; i++) await breaker.recordFailure(`e${i}`);
    expect(await breaker.isOpen()).toBe(true);
    clock.advance(DEFAULT_BREAKER_COOLDOWN_MS - 1);
    expect(await breaker.isOpen()).toBe(true);
    clock.advance(1);
    expect(await breaker.isOpen()).toBe(false);
  });
});
