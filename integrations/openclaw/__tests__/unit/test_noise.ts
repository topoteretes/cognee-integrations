/**
 * Harness-noise detection: the predicate that keeps heartbeat/cron/system
 * template prompts out of recall and QA capture. Two layers under test —
 * ctx.trigger matching and anchored prompt-shape matching — plus the
 * compile step's tolerance of invalid user-supplied patterns.
 */

import { compileNoisePatterns, isHarnessNoise } from "../../src/noise";
import { DEFAULT_NOISE_PATTERNS, DEFAULT_NOISE_TRIGGERS } from "../../src/config";

const defaultRegexes = compileNoisePatterns(DEFAULT_NOISE_PATTERNS);

function noisy(prompt: string, trigger?: string): boolean {
  return isHarnessNoise(prompt, trigger, defaultRegexes, DEFAULT_NOISE_TRIGGERS);
}

describe("isHarnessNoise — trigger layer", () => {
  it("flags heartbeat- and cron-triggered turns regardless of prompt text", () => {
    expect(noisy("anything at all", "heartbeat")).toBe(true);
    expect(noisy("run the nightly digest", "cron")).toBe(true);
  });

  it("does not flag user-triggered or untriggered turns with normal prompts", () => {
    expect(noisy("what did we discuss yesterday?", "user")).toBe(false);
    expect(noisy("what did we discuss yesterday?", undefined)).toBe(false);
  });

  it("respects a custom trigger list, including [] to disable the layer", () => {
    expect(isHarnessNoise("x", "heartbeat", defaultRegexes, [])).toBe(false);
    expect(isHarnessNoise("plain prompt", "digest", defaultRegexes, ["digest"])).toBe(true);
  });
});

describe("isHarnessNoise — prompt-shape layer", () => {
  it("flags OpenClaw's default heartbeat prompt", () => {
    expect(
      noisy(
        "Read HEARTBEAT.md if it exists (workspace context). Follow it strictly. " +
          "Do not infer or repeat old tasks from prior chats. If nothing needs attention, reply HEARTBEAT_OK.",
      ),
    ).toBe(true);
  });

  it("flags System: event lines and [cron:…] payloads", () => {
    expect(noisy("System: exec finished with code 0")).toBe(true);
    expect(noisy("[cron:job-42] nightly cleanup reminder")).toBe(true);
  });

  it("tolerates leading whitespace before the template", () => {
    expect(noisy("  Read HEARTBEAT.md if it exists")).toBe(true);
  });

  it("is anchored: mentioning a template mid-prompt is not noise", () => {
    expect(noisy("why does OpenClaw say Read HEARTBEAT.md every 30 minutes?")).toBe(false);
    expect(noisy("what does the [cron:…] prefix mean?")).toBe(false);
    expect(noisy("the server logs a System: line sometimes")).toBe(false);
  });

  it("an empty pattern list disables the shape layer", () => {
    expect(isHarnessNoise("Read HEARTBEAT.md if it exists", undefined, [], DEFAULT_NOISE_TRIGGERS)).toBe(false);
  });
});

describe("compileNoisePatterns", () => {
  it("skips invalid regexes, reports them, and keeps the rest", () => {
    const warnings: string[] = [];
    const regexes = compileNoisePatterns(["^valid", "([unclosed"], (m) => warnings.push(m));
    expect(regexes).toHaveLength(1);
    expect(regexes[0]!.test("valid prompt")).toBe(true);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain("([unclosed");
  });
});
