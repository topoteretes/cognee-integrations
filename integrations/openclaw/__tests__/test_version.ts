import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  PLUGIN_VERSION,
  compareVersions,
  formatUpdateHint,
  isNewer,
  parseVersion,
  readUpdateCache,
  runUpdateCheck,
  type UpdateCheckRecord,
} from "../src/version";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Locate this package's package.json regardless of the jest working dir. */
function readPackageJson(): { name?: string; version?: string } {
  const candidates = [
    join(process.cwd(), "package.json"),
    join(process.cwd(), "integrations", "openclaw", "package.json"),
  ];
  for (const path of candidates) {
    try {
      const parsed = JSON.parse(readFileSync(path, "utf8"));
      if (parsed?.name === "@cognee/cognee-openclaw") return parsed;
    } catch {
      /* try next candidate */
    }
  }
  throw new Error("could not locate @cognee/cognee-openclaw package.json");
}

function tmpStatePath(): string {
  return join(mkdtempSync(join(tmpdir(), "cognee-version-")), "update-check.json");
}

/** A fetch stub that resolves to a registry-shaped `{ version }` body. */
function okFetch(version: string): typeof fetch {
  return jest.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ version }),
  }) as unknown as typeof fetch;
}

// ---------------------------------------------------------------------------
// Drift guard
// ---------------------------------------------------------------------------

describe("PLUGIN_VERSION", () => {
  it("matches package.json (fallback must never drift from the real version)", () => {
    expect(PLUGIN_VERSION).toBe(readPackageJson().version);
  });
});

// ---------------------------------------------------------------------------
// Version parsing / comparison
// ---------------------------------------------------------------------------

describe("parseVersion", () => {
  it("tolerates a leading v and a pre-release suffix", () => {
    expect(parseVersion("v2026.6.11")).toEqual([2026, 6, 11]);
    expect(parseVersion("2026.6.11-rc1")).toEqual([2026, 6, 11]);
  });
});

describe("compareVersions", () => {
  it("orders numerically, not lexically", () => {
    expect(compareVersions("2026.4.2", "2026.4.12")).toBe(-1);
    expect(compareVersions("2026.4.12", "2026.4.2")).toBe(1);
  });

  it("handles equality and rollover", () => {
    expect(compareVersions("2026.6.11", "2026.6.11")).toBe(0);
    expect(compareVersions("2026.7.0", "2026.6.11")).toBe(1);
    expect(compareVersions("2025.12.9", "2026.1.0")).toBe(-1);
  });
});

describe("isNewer", () => {
  it("is true only when latest is strictly newer", () => {
    expect(isNewer("2026.7.0", "2026.6.11")).toBe(true);
    expect(isNewer("2026.6.11", "2026.6.11")).toBe(false);
    expect(isNewer("2026.5.0", "2026.6.11")).toBe(false);
  });

  it("never reports an update when either version is unknown", () => {
    expect(isNewer("", "2026.6.11")).toBe(false);
    expect(isNewer("2026.7.0", "")).toBe(false);
  });
});

describe("formatUpdateHint", () => {
  it("mentions the version and the install command", () => {
    const hint = formatUpdateHint("2026.7.2");
    expect(hint).toContain("2026.7.2");
    expect(hint).toContain("@cognee/cognee-openclaw@latest");
  });
});

// ---------------------------------------------------------------------------
// runUpdateCheck
// ---------------------------------------------------------------------------

describe("runUpdateCheck", () => {
  const enabledEnv = {} as NodeJS.ProcessEnv;

  it("writes an available update from the registry", async () => {
    const statePath = tmpStatePath();
    const fetchImpl = okFetch("2099.1.1");
    const record = await runUpdateCheck({
      installed: "2026.6.11",
      statePath,
      force: true,
      env: enabledEnv,
      fetchImpl,
    });

    expect(record).toMatchObject({
      installed: "2026.6.11",
      latest: "2099.1.1",
      updateAvailable: true,
    });
    const onDisk = JSON.parse(readFileSync(statePath, "utf8")) as UpdateCheckRecord;
    expect(onDisk.updateAvailable).toBe(true);
  });

  it("reports no update when versions are equal", async () => {
    const record = await runUpdateCheck({
      installed: "2026.6.11",
      statePath: tmpStatePath(),
      force: true,
      env: enabledEnv,
      fetchImpl: okFetch("2026.6.11"),
    });
    expect(record?.updateAvailable).toBe(false);
  });

  it("returns null (and does nothing) when disabled", async () => {
    const fetchImpl = jest.fn() as unknown as typeof fetch;
    const record = await runUpdateCheck({
      installed: "2026.6.11",
      statePath: tmpStatePath(),
      force: true,
      env: { COGNEE_UPDATE_CHECK: "false" } as NodeJS.ProcessEnv,
      fetchImpl,
    });
    expect(record).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("skips the network within the TTL", async () => {
    const statePath = tmpStatePath();
    writeFileSync(
      statePath,
      JSON.stringify({ checkedAt: 1_000_000, installed: "2026.6.11", latest: "9.9.9", updateAvailable: true }),
    );
    const fetchImpl = jest.fn(() => {
      throw new Error("should not hit the network within the TTL");
    }) as unknown as typeof fetch;

    const record = await runUpdateCheck({
      installed: "2026.6.11",
      statePath,
      env: enabledEnv,
      now: () => 1_000_000 + 60_000, // one minute later, well within 24h
      fetchImpl,
    });

    expect(fetchImpl).not.toHaveBeenCalled();
    expect(record?.latest).toBe("9.9.9");
  });

  it("preserves the previous latest on a network failure", async () => {
    const statePath = tmpStatePath();
    writeFileSync(
      statePath,
      JSON.stringify({ checkedAt: 0, installed: "2026.6.11", latest: "2026.9.9", updateAvailable: true }),
    );
    const fetchImpl = jest.fn().mockRejectedValue(new Error("offline")) as unknown as typeof fetch;

    const record = await runUpdateCheck({
      installed: "2026.6.11",
      statePath,
      force: true,
      env: enabledEnv,
      fetchImpl,
    });

    expect(record?.latest).toBe("2026.9.9");
    expect(record?.updateAvailable).toBe(true);
  });
});

describe("readUpdateCache", () => {
  it("returns null when the cache file is missing", async () => {
    expect(await readUpdateCache(join(tmpdir(), "does-not-exist-cognee.json"))).toBeNull();
  });
});
