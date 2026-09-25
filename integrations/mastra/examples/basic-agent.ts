/**
 * Basic usage: `withCognee()` on a Mastra `Agent` — automatic recall
 * (pre-turn) and capture (post-turn), no change to conversation flow. Run:
 * `docker compose -f examples/docker-compose.smoke.yml up`, then
 * `npx tsx examples/basic-agent.ts`.
 *
 * Only fires when the agent also has a Mastra `Memory` attached and the call
 * passes `memory: { thread, resource }` — `withCognee()` appends processors only.
 */

import { Agent, type AgentConfig } from "@mastra/core/agent";
// `@mastra/memory` is a peer of `@mastra/core`, not a dependency of this
// package — install it in your own project (`npm i @mastra/memory`) the
// same way you would for any other Mastra agent that wants message
// persistence. Not present in this monorepo's own devDependencies (this
// example is excluded from this package's own `tsconfig.json`/`tsc` run —
// see the top-level `exclude: ["examples"]` — precisely so a peer this
// package itself never imports doesn't have to be installed just to build
// the library).
import { Memory } from "@mastra/memory";

import { withCognee } from "../index.js";

// Declared as its own typed constant (rather than an inline object literal
// passed straight into `withCognee()`) so TypeScript infers `withCognee`'s
// generic parameter from the full `AgentConfig` shape below, instead of
// falling back to its `Partial<ProcessorFields>` constraint and rejecting
// `id`/`name`/`model`/`memory` as excess properties on the literal.
const baseAgentConfig: AgentConfig = {
  id: "support-agent",
  name: "Support Agent",
  instructions:
    "You are a helpful support agent. Use anything cognee recalls about this user or thread as background " +
    "context, but always answer the user's actual question directly.",
  model: "openai/gpt-5.2",
  // A bare Memory is enough to plumb thread/resource ids through to
  // cognee's processors, even if you don't otherwise rely on Mastra's own
  // message history — see the file header note above.
  memory: new Memory(),
};

export const supportAgent = new Agent(
  withCognee(
    baseAgentConfig,
    {
      // Explicit config here wins over COGNEE_* env vars, which win over
      // this package's own defaults.
      baseUrl: process.env.COGNEE_API_URL ?? "http://localhost:8000",
      apiKey: process.env.COGNEE_API_KEY,
      dataset: "support-agent",
      recall: {
        // CHUNKS is already this package's default for the automatic path
        // (no per-turn LLM cost inside cognee) — spelled out here for clarity.
        searchType: "CHUNKS",
        topK: 5,
      },
      write: {
        // Only capture the assistant's own answers automatically; skip
        // re-ingesting the user's question verbatim (it's already in
        // Mastra's own message history via `memory: new Memory()` above).
        mode: "assistant-only",
      },
    },
  ),
);

async function main(): Promise<void> {
  const threadId = "example-thread-1";
  const resourceId = "example-user-1";

  const first = await supportAgent.generate([{ role: "user", content: "I prefer dark mode and TypeScript." }], {
    memory: { thread: threadId, resource: resourceId },
  });
  console.log("assistant:", first.text);

  // A later turn, possibly in a different process/session, same thread +
  // resource: the input processor recalls what cognee has ingested so far
  // (subject to cognee's own ingest -> cognify -> recallable latency — see
  // the README's "Failure behaviour" note; this is not immediate).
  const second = await supportAgent.generate([{ role: "user", content: "What editor theme do I use?" }], {
    memory: { thread: threadId, resource: resourceId },
  });
  console.log("assistant:", second.text);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error: unknown) => {
    console.error(error);
    process.exitCode = 1;
  });
}
