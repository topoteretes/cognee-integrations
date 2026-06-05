import { createHash } from "node:crypto";
import { type Plugin, tool } from "@opencode-ai/plugin";
import { CogneeHttpClient } from "./client.js";
import type { CogneePluginConfig } from "./types.js";

function resolveConfig(customConfig?: Partial<CogneePluginConfig>): Required<CogneePluginConfig> {
  const mode = (process.env.COGNEE_MODE || customConfig?.mode || "local") as "local" | "cloud";
  return {
    mode,
    baseUrl: process.env.COGNEE_SERVICE_URL || customConfig?.baseUrl || (mode === "cloud" ? "https://api.cognee.ai" : "http://localhost:8000"),
    apiKey: process.env.COGNEE_API_KEY || customConfig?.apiKey || "",
    username: process.env.COGNEE_USERNAME || customConfig?.username || "default_user@example.com",
    password: process.env.COGNEE_PASSWORD || customConfig?.password || "default_password",
    datasetName: process.env.COGNEE_PLUGIN_DATASET || customConfig?.datasetName || "opencode_sessions",
    enableSessions: process.env.COGNEE_ENABLE_SESSIONS !== "false" && (customConfig?.enableSessions !== false),
    searchType: (customConfig?.searchType || "GRAPH_COMPLETION"),
    searchPrompt: customConfig?.searchPrompt || "",
    deleteMode: customConfig?.deleteMode || "soft",
    maxResults: customConfig?.maxResults || 5,
    minScore: customConfig?.minScore || 0.1,
    maxTokens: customConfig?.maxTokens || 1000,
    autoRecall: customConfig?.autoRecall !== false,
    improveOnSessionEnd: customConfig?.improveOnSessionEnd !== false,
    requestTimeoutMs: customConfig?.requestTimeoutMs || 60000,
    ingestionTimeoutMs: customConfig?.ingestionTimeoutMs || 300000,
  };
}

export const CogneeOpenCodePlugin: Plugin = async (ctx, options) => {
  const config = resolveConfig((ctx.project as any)?.config?.cognee || options?.cognee);
  const client = new CogneeHttpClient(
    config.baseUrl,
    config.apiKey,
    config.username,
    config.password,
    config.requestTimeoutMs,
    config.ingestionTimeoutMs,
    config.mode,
  );

  // Isolate session per workspace directory using a hash of the CWD
  const dirHash = createHash("sha256").update(ctx.directory || process.cwd()).digest("hex").slice(0, 8);
  const projectId = (ctx.project as any)?.id || (ctx.project as any)?.projectID || "default";
  let sessionId = config.enableSessions ? `oc_${projectId}_${dirHash}` : undefined;

  let lastUserPrompt = "recalled context";

  // Verify connection at startup
  client.health().catch((err) => {
    console.warn(`cognee-opencode: failed to connect to Cognee service: ${String(err)}`);
  });

  const IGNORED_TOOLS = new Set([
    "view_file",
    "read_file",
    "read_url_content",
    "read_browser_page",
    "grep_search",
    "list_dir",
    "list_pages",
  ]);

  return {
    // Keep track of user messages to use as recall query text
    "chat.message": async (input, output) => {
      const textParts = output.parts
        .filter((p: any) => p.type === "text" && typeof p.text === "string")
        .map((p: any) => p.text)
        .join(" ");
      if (textParts.trim().length > 0) {
        lastUserPrompt = textParts;
      }
    },

    // 1. Auto-capture tool use
    "tool.execute.after": async (input, output) => {
      if (IGNORED_TOOLS.has(input.tool)) {
        return;
      }
      try {
        let toolOutput = typeof output.output === "string" ? output.output : JSON.stringify(output.output);
        if (toolOutput.length > 8000) {
          toolOutput = toolOutput.slice(0, 8000) + "... [truncated]";
        }
        const memoryPayload = `Tool: ${input.tool}\nArguments: ${JSON.stringify(input.args)}\nOutput: ${toolOutput}`;
        await client.remember({
          data: memoryPayload,
          datasetName: config.datasetName,
          sessionId,
          nodeSet: ["agent_actions"],
        });
      } catch (err) {
        console.warn(`cognee-opencode: auto-capture failed: ${String(err)}`);
      }
    },

    // 2. Auto-recall during compaction (experimental hook)
    "experimental.session.compacting": async (input, output) => {
      if (!config.autoRecall) return;
      try {
        const datasetList = await client.listDatasets();
        const dataset = datasetList.find((d) => d.name === config.datasetName);
        if (!dataset) return;

        const memories = await client.recall({
          queryText: lastUserPrompt,
          datasetIds: [dataset.id],
          searchType: config.searchType,
          topK: config.maxResults,
          sessionId,
        });

        const filtered = memories.filter((m) => m.score >= config.minScore);
        if (filtered.length > 0) {
          const contextPayload = filtered.map((m) => `- ${m.text}`).join("\n");
          output.context.push(`## Cognee Recalled Memories\n\n${contextPayload}`);
        }
      } catch (err) {
        console.warn(`cognee-opencode: auto-recall failed: ${String(err)}`);
      }
    },

    // 3. Improve on session completion/idle
    event: async ({ event }) => {
      if (config.improveOnSessionEnd && event.type === "session.idle") {
        try {
          await client.improve({
            datasetName: config.datasetName,
            sessionIds: sessionId ? [sessionId] : undefined,
          });
        } catch (err) {
          console.warn(`cognee-opencode: session improve failed: ${String(err)}`);
        }
      }
    },

    // 4. Custom tools
    tool: {
      cognee_remember: tool({
        description: "Save custom facts, user preferences, or project details into long-term Cognee memory",
        args: {
          fact: tool.schema.string().describe("The fact, preference, or information to remember"),
          category: tool.schema.enum(["user", "project", "agent"]).optional().describe("Memory classification category"),
        },
        async execute(args) {
          try {
            await client.remember({
              data: args.fact,
              datasetName: config.datasetName,
              sessionId,
              nodeSet: args.category ? [`${args.category}_context`] : undefined,
            });
            return `Successfully saved fact to Cognee memory: "${args.fact}"`;
          } catch (err) {
            return `Failed to save fact: ${err instanceof Error ? err.message : String(err)}`;
          }
        },
      }),

      cognee_search: tool({
        description: "Explicitly search the Cognee memory graph for relevant context on a topic",
        args: {
          query: tool.schema.string().describe("The search query terms or phrase"),
        },
        async execute(args) {
          try {
            const datasetList = await client.listDatasets();
            const dataset = datasetList.find((d) => d.name === config.datasetName);
            if (!dataset) {
              return "No Cognee memory dataset found. Try remembering something first.";
            }

            const results = await client.recall({
              queryText: args.query,
              datasetIds: [dataset.id],
              searchType: config.searchType,
              topK: config.maxResults,
              sessionId,
            });

            if (results.length === 0) {
              return "No relevant memories found in Cognee graph.";
            }

            return JSON.stringify(
              results.map((r) => ({ text: r.text, score: r.score })),
              null,
              2,
            );
          } catch (err) {
            return `Failed to search Cognee memory: ${err instanceof Error ? err.message : String(err)}`;
          }
        },
      }),
    },
  };
};
