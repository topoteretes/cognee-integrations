/**
 * `withCognee(agentConfig, config)` — convenience wrapper that merges
 * cognee's input/output processors into an `Agent` config (precedent:
 * Supermemory's `withSupermemory()`).
 *
 * Merge semantics mirror the idiom Mastra's own `Agent` class uses
 * internally to merge signal-provider processors onto a user-supplied
 * `inputProcessors`/`outputProcessors` value: both fields are either a plain
 * array or a `(ctx: { requestContext }) => Processor[] | Promise<Processor[]>`
 * function, so a correct merge has to handle both shapes.
 */

import type { AgentConfig } from "@mastra/core/agent";
import type { InputProcessorOrWorkflow, OutputProcessorOrWorkflow } from "@mastra/core/processors";

import { createCogneeProcessors, type CogneeProcessorsConfig } from "./processors.js";

/**
 * The slice of `AgentConfig` this helper reads/writes. Typed as a `Pick` off
 * the real `AgentConfig` rather than redeclared, so a signature change to
 * either field upstream is a type error here instead of a silent drift.
 */
type ProcessorFields = Pick<AgentConfig, "inputProcessors" | "outputProcessors">;

/**
 * Append `extra` to whichever shape `existing` already is. `undefined` (no
 * existing processors) short-circuits to `extra` itself rather than
 * wrapping a trivial array in a needless function.
 */
function appendInput(
  existing: AgentConfig["inputProcessors"],
  extra: InputProcessorOrWorkflow[],
): AgentConfig["inputProcessors"] {
  if (!existing) return extra;
  if (typeof existing === "function") {
    return async (ctx) => {
      const resolved = await existing(ctx);
      return [...resolved, ...extra];
    };
  }
  return [...existing, ...extra];
}

/** Symmetric to `appendInput`, for `outputProcessors`. */
function appendOutput(
  existing: AgentConfig["outputProcessors"],
  extra: OutputProcessorOrWorkflow[],
): AgentConfig["outputProcessors"] {
  if (!existing) return extra;
  if (typeof existing === "function") {
    return async (ctx) => {
      const resolved = await existing(ctx);
      return [...resolved, ...extra];
    };
  }
  return [...existing, ...extra];
}

/**
 * Merges `createCogneeProcessors(config)`'s `input`/`output` processors into
 * `agentConfig`, appending to whatever `inputProcessors`/`outputProcessors`
 * the caller already has (array or function form), and returns a new config
 * object — `agentConfig` itself is not mutated.
 *
 * Ordering: cognee's input processor is appended after the caller's existing
 * input processors (so user-authored recall/guardrail processors still see
 * the raw query unmodified), and cognee's output processor is appended after
 * the caller's existing output processors (so it captures the final,
 * fully-processed turn).
 *
 * `withCognee()` only wires the processors: they stay inert until the
 * resulting `agentConfig` is used with an agent that has some Mastra
 * `Memory` attached and is called with a `threadId`/`resourceId`.
 */
export function withCognee<T extends Partial<ProcessorFields>>(
  agentConfig: T,
  config: CogneeProcessorsConfig = {},
): T & ProcessorFields {
  const { input, output } = createCogneeProcessors(config);
  return {
    ...agentConfig,
    inputProcessors: appendInput(agentConfig.inputProcessors as AgentConfig["inputProcessors"], [input]),
    outputProcessors: appendOutput(agentConfig.outputProcessors as AgentConfig["outputProcessors"], [output]),
  };
}
