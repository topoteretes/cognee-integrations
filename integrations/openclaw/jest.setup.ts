/**
 * Suite-wide sandbox for `os.homedir()`.
 *
 * The plugin persists everything under the home directory —
 * `~/.openclaw/memory/cognee/*.json` (sync state) and `~/.cognee-plugin`
 * (api key, boot script) — and resolves those paths through `os.homedir()` at
 * module load. Any test that drives the plugin far enough to save state therefore
 * writes into the developer's real home unless something stops it.
 *
 * Something now does. Every test file gets `homedir()` pointed at a throwaway
 * directory, so reaching the real home is impossible rather than merely
 * discouraged.
 *
 * This is a backstop, not a nicety: it was added after `test_cliCommands.ts`
 * overwrote a real `~/.openclaw/memory/cognee/` — the CLI actions call
 * `saveDatasetState` through the genuine persistence module, which happily wrote
 * to the true path. Per-file discipline had already failed once, so the default
 * had to change.
 *
 * Note that setting `process.env.HOME` does NOT work here: jest gives each test
 * file its own `process.env`, and mutating it never reaches the C-level `getenv`
 * that libuv's `uv_os_homedir` reads. Mocking the module is the only reliable
 * redirect.
 *
 * A test needing per-case control (see `__tests__/unit/test_persistence.ts`)
 * declares its own `jest.mock("node:os", ...)`, which takes precedence.
 */

import { mkdtempSync } from "node:fs";
import { join } from "node:path";

const actualOs = jest.requireActual<typeof import("node:os")>("node:os");

/** One sandbox home per test file (each gets its own module registry). */
const sandboxHome = mkdtempSync(join(actualOs.tmpdir(), "cognee-openclaw-sandbox-home-"));

jest.mock("node:os", () => ({
  ...jest.requireActual<typeof import("node:os")>("node:os"),
  homedir: () => sandboxHome,
}));

// Surfaced so a failing test can report where its state actually went.
(globalThis as Record<string, unknown>).__COGNEE_SANDBOX_HOME__ = sandboxHome;
