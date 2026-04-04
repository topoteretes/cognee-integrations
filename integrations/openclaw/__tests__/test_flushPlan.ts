import type { OpenClawConfig } from "openclaw/plugin-sdk";
import { buildMemoryFlushPlan } from "../src/flush-plan";

function createConfig(overrides?: Partial<OpenClawConfig>): OpenClawConfig {
  return {
    agents: {
      defaults: {
        compaction: {},
        userTimezone: "Asia/Shanghai",
        timeFormat: "24",
      },
    },
    ...overrides,
  } as OpenClawConfig;
}

describe("buildMemoryFlushPlan", () => {
  it("returns a default plan when enabled", () => {
    const plan = buildMemoryFlushPlan({
      cfg: createConfig(),
      nowMs: Date.UTC(2026, 3, 3, 3, 4, 5),
    });

    expect(plan).not.toBeNull();
    expect(plan?.relativePath).toBe("memory/2026-04-03.md");
    expect(plan?.softThresholdTokens).toBe(4000);
    expect(plan?.forceFlushTranscriptBytes).toBe(2 * 1024 * 1024);
    expect(plan?.prompt).toContain("NO_REPLY");
    expect(plan?.prompt).toContain("Current time:");
    expect(plan?.systemPrompt).toContain("memory/2026-04-03.md");
  });

  it("returns null when memory flush is disabled", () => {
    const plan = buildMemoryFlushPlan({
      cfg: createConfig({
        agents: {
          defaults: {
            compaction: {
              memoryFlush: { enabled: false },
            },
          },
        },
      }),
    });

    expect(plan).toBeNull();
  });

  it("respects custom compaction thresholds and prompts", () => {
    const plan = buildMemoryFlushPlan({
      cfg: createConfig({
        agents: {
          defaults: {
            userTimezone: "UTC",
            timeFormat: "24",
            compaction: {
              reserveTokensFloor: 1234,
              memoryFlush: {
                softThresholdTokens: 567,
                forceFlushTranscriptBytes: "3mb",
                prompt: "Custom flush for YYYY-MM-DD",
                systemPrompt: "System flush for YYYY-MM-DD",
              } as unknown as OpenClawConfig["agents"]["defaults"]["compaction"]["memoryFlush"],
            },
          },
        },
      }),
      nowMs: Date.UTC(2026, 3, 3, 0, 0, 0),
    });

    expect(plan).not.toBeNull();
    expect(plan?.softThresholdTokens).toBe(567);
    expect(plan?.forceFlushTranscriptBytes).toBe(3 * 1024 * 1024);
    expect(plan?.reserveTokensFloor).toBe(1234);
    expect(plan?.prompt).toContain("Custom flush for 2026-04-03");
    expect(plan?.prompt).toContain("Store durable memories only in memory/2026-04-03.md");
    expect(plan?.systemPrompt).toContain("System flush for 2026-04-03");
    expect(plan?.systemPrompt).toContain("APPEND new content only");
    expect(plan?.systemPrompt).toContain("NO_REPLY");
  });
});
