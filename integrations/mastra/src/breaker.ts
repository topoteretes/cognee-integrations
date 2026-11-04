/**
 * File-backed circuit breaker for cognee's recall path: only answers "is
 * the breaker open" / "record a failure" / "record a success". Classifying
 * which HTTP failures are breaker-eligible (5xx/network vs 4xx) belongs to
 * `errors.ts`'s `isBreakerError`, keeping that logic in one place.
 */

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { homedir } from "node:os";

/** 5 consecutive failures trip the breaker. */
export const DEFAULT_BREAKER_THRESHOLD = 5;
/** 120s cooldown once tripped. */
export const DEFAULT_BREAKER_COOLDOWN_MS = 120_000;

/**
 * `~/.cognee-mastra/recall-breaker.json` — package-scoped, not shared with
 * other Cognee integrations' breakers, so one unrelated tool's failures
 * can't trip this one. Resolved at call time (not module load) so per-test `os.homedir()`
 * sandboxing always applies.
 */
export function defaultBreakerPath(): string {
  return join(homedir(), ".cognee-mastra", "recall-breaker.json");
}

interface BreakerState {
  failures: number;
  cooldownUntilMs: number;
  lastError?: string;
}

const CLOSED_STATE: Readonly<BreakerState> = Object.freeze({ failures: 0, cooldownUntilMs: 0 });

function isPlausibleState(value: unknown): value is { failures: number; cooldownUntilMs: number; lastError?: unknown } {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.failures === "number" &&
    Number.isFinite(v.failures) &&
    typeof v.cooldownUntilMs === "number" &&
    Number.isFinite(v.cooldownUntilMs)
  );
}

export interface CircuitBreakerOptions {
  /** Consecutive failures before the breaker opens. Default 5. */
  threshold?: number;
  /** Cooldown once opened, in ms. Default 120_000 (120s). */
  cooldownMs?: number;
  /** State file path. Default `~/.cognee-mastra/recall-breaker.json`. */
  path?: string;
  /**
   * Clock — injectable so a test can pass `now: () => t` and move `t` forward deterministically.
   * Default `Date.now`.
   */
  now?: () => number;
}

/**
 * Cheap to construct, stateless in memory — every call re-reads the state file, so multiple
 * instances (or processes) sharing `path` observe and contribute to the same trip/cooldown.
 */
export class CircuitBreaker {
  private readonly threshold: number;
  private readonly cooldownMs: number;
  private readonly path: string;
  private readonly now: () => number;

  constructor(options: CircuitBreakerOptions = {}) {
    this.threshold = options.threshold ?? DEFAULT_BREAKER_THRESHOLD;
    this.cooldownMs = options.cooldownMs ?? DEFAULT_BREAKER_COOLDOWN_MS;
    this.path = options.path ?? defaultBreakerPath();
    this.now = options.now ?? Date.now;
  }

  /**
   * Missing file, unreadable file, invalid JSON, or a shape that doesn't look like `BreakerState`
   * all degrade to the closed state — a breaker must never fail open merely because its own
   * bookkeeping file is broken.
   */
  private async load(): Promise<BreakerState> {
    let raw: unknown;
    try {
      raw = JSON.parse(await readFile(this.path, "utf-8"));
    } catch {
      return { ...CLOSED_STATE };
    }
    if (!isPlausibleState(raw)) return { ...CLOSED_STATE };
    return {
      failures: raw.failures,
      cooldownUntilMs: raw.cooldownUntilMs,
      lastError: typeof raw.lastError === "string" ? raw.lastError : undefined,
    };
  }

  /**
   * Best-effort persistence — a write failure (e.g. read-only home) must never throw into a
   * caller's request path.
   */
  private async save(state: BreakerState): Promise<void> {
    try {
      await mkdir(dirname(this.path), { recursive: true });
      await writeFile(this.path, JSON.stringify(state), "utf-8");
    } catch {
      // best-effort; the in-memory decision for *this* call already happened.
    }
  }

  /** Whether the breaker currently blocks calls (i.e. still within cooldown). */
  async isOpen(): Promise<boolean> {
    const state = await this.load();
    return state.cooldownUntilMs > this.now();
  }

  /** Milliseconds remaining until the breaker allows calls again; 0 when closed or half-open. */
  async remainingCooldownMs(): Promise<number> {
    const state = await this.load();
    const remaining = state.cooldownUntilMs - this.now();
    return remaining > 0 ? remaining : 0;
  }

  /**
   * Opens for `cooldownMs` once `failures` reaches `threshold`. Failures
   * aren't reset on trip: after cooldown elapses the breaker half-opens, and one failed probe
   * re-trips immediately rather than requiring `threshold` fresh failures.
   */
  async recordFailure(error?: unknown): Promise<void> {
    const state = await this.load();
    state.failures += 1;
    const message = toMessage(error);
    if (message !== undefined) state.lastError = message.slice(0, 300);
    if (state.failures >= this.threshold) {
      state.cooldownUntilMs = this.now() + this.cooldownMs;
    }
    await this.save(state);
  }

  /** Record a success: fully resets the breaker (failures and cooldown both cleared). */
  async recordSuccess(): Promise<void> {
    const state = await this.load();
    if (state.failures === 0 && state.cooldownUntilMs === 0) return; // avoid write churn on the happy path
    await this.save({ ...CLOSED_STATE });
  }
}

/**
 * Redacts credential-shaped substrings before persisting an error to the
 * breaker's state file — `CogneeApiError.message` can carry text lifted
 * straight from a server's JSON body, so this defends by pattern rather than by tracking specific
 * known secrets.
 */
const SENSITIVE_PATTERN = /\b(api[_-]?key|password|access[_-]?token|bearer|authorization)\b\s*[:=]\s*\S+/gi;

function sanitizeErrorMessage(message: string): string {
  return message.replace(SENSITIVE_PATTERN, (_match, key: string) => `${key}=***REDACTED***`);
}

function toMessage(error: unknown): string | undefined {
  if (error === undefined) return undefined;
  const raw = error instanceof Error ? error.message : String(error);
  return sanitizeErrorMessage(raw);
}
