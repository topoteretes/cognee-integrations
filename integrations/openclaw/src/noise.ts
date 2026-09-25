// ---------------------------------------------------------------------------
// Harness-noise detection
//
// OpenClaw drives agents with synthetic prompts the user never typed:
// heartbeat probes ("Read HEARTBEAT.md if it exists…"), cron payloads, and
// "System: …" event lines. Those are host instructions, not memory queries —
// recalling on them burns LLM-backed searches per scope, and capturing them
// as QA pairs bridges harness templates into the permanent graph at session
// end, degrading recall quality for real prompts.
//
// Detection is two-layered:
//   1. ctx.trigger — OpenClaw stamps hook contexts with the run trigger
//      ("heartbeat", "cron", "user", …). Authoritative when present.
//   2. Prompt shape — anchored regexes over the prompt text, for hosts that
//      don't populate ctx.trigger and for system-event lines that arrive on
//      user-triggered runs.
// ---------------------------------------------------------------------------

/**
 * Compile pattern strings into regexes, skipping (and reporting) invalid
 * ones so a single bad user-supplied pattern can't disable the plugin.
 */
export function compileNoisePatterns(
  patterns: readonly string[],
  warn?: (message: string) => void,
): RegExp[] {
  const compiled: RegExp[] = [];
  for (const pattern of patterns) {
    try {
      compiled.push(new RegExp(pattern));
    } catch (e) {
      warn?.(`cognee-openclaw: invalid noisePatterns entry ${JSON.stringify(pattern)} skipped: ${String(e)}`);
    }
  }
  return compiled;
}

/**
 * True when a prompt is harness-generated noise that should be excluded from
 * recall and session capture. `trigger` is the hook context's run trigger
 * (may be absent on older hosts); patterns match against the prompt with
 * leading whitespace stripped so `^`-anchored shapes behave as prefixes.
 */
export function isHarnessNoise(
  prompt: string,
  trigger: string | undefined,
  patterns: readonly RegExp[],
  triggers: readonly string[],
): boolean {
  if (trigger && triggers.includes(trigger)) return true;
  const text = prompt.trimStart();
  return patterns.some((re) => re.test(text));
}
