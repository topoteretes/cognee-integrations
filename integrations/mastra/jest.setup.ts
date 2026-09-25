/**
 * Suite-wide sandbox for `os.homedir()`. `src/breaker.ts` persists circuit
 * breaker state under the developer's home directory, so any test that
 * trips or persists it would write there unless redirected here instead.
 * Setting `process.env.HOME` does not work: jest gives each test file its
 * own `process.env`, which never reaches the C-level `getenv` libuv's
 * `uv_os_homedir` reads.
 */

// Imported explicitly rather than used as an ambient global: under this
// package's ESM preset, Jest doesn't inject `jest` into module scope the way
// it does for CommonJS, so a bare `jest.mock(...)` reference throws
// ReferenceError here.
import { jest } from "@jest/globals";
import { mkdtempSync } from "node:fs";
import { join } from "node:path";

const actualOs = jest.requireActual<typeof import("node:os")>("node:os");

/** One sandbox home per test file (each gets its own module registry). */
const sandboxHome = mkdtempSync(join(actualOs.tmpdir(), "cognee-mastra-sandbox-home-"));

// A test needing per-case control can declare its own `jest.mock("node:os",
// ...)`, which wins over this one.
jest.mock("node:os", () => ({
  ...jest.requireActual<typeof import("node:os")>("node:os"),
  homedir: () => sandboxHome,
}));

// Surfaced so a failing test can report where its state went.
(globalThis as Record<string, unknown>).__COGNEE_SANDBOX_HOME__ = sandboxHome;
