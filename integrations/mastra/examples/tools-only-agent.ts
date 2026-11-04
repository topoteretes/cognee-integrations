/**
 * Tools-only usage: `createCogneeTools()` as ordinary `Agent` tools, no
 * input/output processors wired in — the model decides when to search, ask,
 * or write to cognee itself. Independent of `createCogneeProcessors`/
 * `withCognee` (see `examples/basic-agent.ts` for that path).
 *
 * Run: `docker compose -f examples/docker-compose.smoke.yml up`, then
 * `npx tsx examples/tools-only-agent.ts`.
 */

import { Agent } from "@mastra/core/agent";

import { createCogneeTools } from "../index.js";

const cogneeTools = createCogneeTools({
  baseUrl: process.env.COGNEE_API_URL ?? "http://localhost:8000",
  apiKey: process.env.COGNEE_API_KEY,
  dataset: "tools-only-agent",
  // Destructive and opt-in only — omit this entirely unless the agent needs
  // to delete specific memories on explicit user request. Left here only to
  // show the knob exists.
  tools: { enableForget: false },
});

export const researchAgent = new Agent({
  id: "research-agent",
  name: "Research Agent",
  instructions:
    "You have three cognee tools: cognee_search (fast, cheap, use it first for lookups), cognee_ask (slow and " +
    "expensive — an LLM call runs inside cognee — only use it when you need graph reasoning over a " +
    "written synthesis, not a quick fact check), and cognee_remember (call it explicitly when the user asks you " +
    "to remember something, or when you learn a durable fact worth keeping beyond this conversation).",
  model: "openai/gpt-5.2",
  tools: cogneeTools,
  // No `memory` field at all — this agent has no message history of its
  // own. That's fine for the tools-only surface (unlike `withCognee()`'s
  // processors, `createCogneeTools()` does not need a Mastra `Memory`
  // instance or a `memory: { thread, resource }` call option to function —
  // see `src/processors.ts`'s file header for why that requirement is
  // specific to the processor surface).
});

async function main(): Promise<void> {
  const result = await researchAgent.generate([
    { role: "user", content: "Remember that our deployment target is Railway, then tell me what you just saved." },
  ]);
  console.log("assistant:", result.text);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error: unknown) => {
    console.error(error);
    process.exitCode = 1;
  });
}
