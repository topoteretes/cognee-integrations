/**
 * cognee_recall tool — model-visible tool that searches Cognee memory.
 *
 * The model can call this tool for deeper or cross-session searches
 * when the automatic context injected by the user-prompt-submit hook
 * is insufficient. All calls go through the in-process cognee-client
 * (no subprocess, no shell).
 */

import { RiskLevel, type ToolDefinition, type ToolExecutionResult } from "@vellumai/plugin-api";

import {
  loadConfig,
  resolveSessionId,
  sanitizeSessionKey,
  resolveHttpEndpoint,
} from "../src/plugin-common.ts";
import { recall, UNREACHABLE, type RecallResult } from "../src/cognee-client.ts";

interface RecallParams {
  query: string;
  top_k?: number;
  scope?: "session" | "graph" | "auto";
}

export const cogneeRecall: ToolDefinition = {
  name: "cognee_recall",
  description:
    "Search Cognee memory (session cache and permanent knowledge graph) to retrieve relevant context. " +
    "Session memory is auto-searched on every prompt via hooks; use this tool for deeper or cross-session searches. " +
    "Can filter by scope: 'session' for current session only, 'graph' for permanent graph only, 'auto' for both.",
  input_schema: {
    type: "object",
    properties: {
      query: {
        type: "string",
        description: "The search query — a natural language question or keywords.",
      },
      top_k: {
        type: "number",
        description: "Maximum number of results to return (default: 10).",
      },
      scope: {
        type: "string",
        enum: ["session", "graph", "auto"],
        description:
          "Search scope: 'session' for current session cache, 'graph' for permanent knowledge graph, 'auto' for both (default).",
      },
    },
    required: ["query"],
  },
  defaultRiskLevel: RiskLevel.Low,

  async execute(
    input: Record<string, unknown>,
  ): Promise<ToolExecutionResult> {
    const params = input as unknown as RecallParams;
    const query = params.query;
    if (!query) {
      return { content: "Error: no query provided", isError: true };
    }

    const cfg = loadConfig();
    const { baseUrl, apiKey } = resolveHttpEndpoint();

    if (!apiKey) {
      return {
        content:
          "Cognee search failed: no API key configured. Set COGNEE_API_KEY or ensure the local cognee server is running.",
        isError: true,
      };
    }

    // Resolve session from conversationId if available in env.
    const sessionKey = sanitizeSessionKey(process.env.COGNEE_SESSION_KEY ?? "");
    const sessionId = sessionKey ? resolveSessionId(sessionKey, cfg.agentName) : "";

    const topK = params.top_k ?? 10;
    const scopeParam = params.scope ?? "auto";
    const scope =
      scopeParam === "session"
        ? ["session"]
        : scopeParam === "graph"
          ? ["graph"]
          : ["session", "graph"];

    try {
      const result = await recall(
        baseUrl,
        apiKey,
        query,
        sessionId,
        scope,
        topK,
        cfg.dataset,
      );

      if (result === UNREACHABLE) {
        return {
          content:
            "Cognee search failed: server unreachable. The server may be down or not yet started.",
          isError: true,
        };
      }

      if (Array.isArray(result)) {
        if (result.length === 0) {
          return {
            content:
              "No results found. The server returned an empty list (authoritative). " +
              "Try using the cognee-sync skill to sync session data to the permanent graph, " +
              "or the cognee-remember skill to ingest new data.",
            isError: false,
          };
        }

        // Format results for the model.
        const lines: string[] = [];
        for (const entry of result) {
          const e = entry as Record<string, unknown>;
          const source = (e._source as string) ?? (e.source as string) ?? "unknown";
          const question = (e.question as string) ?? "";
          const answer = (e.answer as string) ?? "";
          const content = (e.content as string) ?? (e.text as string) ?? "";
          const origin = (e.origin_function as string) ?? "";
          const status = (e.status as string) ?? "";

          if (source === "graph_context" || source === "graph") {
            lines.push(`[graph] ${content.slice(0, 500)}`);
          } else if (source === "trace") {
            lines.push(`[trace] ${origin} — ${status}`);
          } else if (question || answer) {
            lines.push(`[session] Q: ${question.slice(0, 200)}`);
            if (answer) lines.push(`  A: ${answer.slice(0, 300)}`);
          } else if (content) {
            lines.push(`[${source}] ${content.slice(0, 500)}`);
          }
        }

        return {
          content: `Cognee memory search results (${result.length} hits):\n\n${lines.join("\n")}`,
          isError: false,
        };
      }

      // Error envelope from the server.
      const errObj = result as Record<string, unknown>;
      if (errObj.error) {
        return {
          content: `Cognee search error: ${errObj.error}`,
          isError: true,
        };
      }

      return {
        content: JSON.stringify(result, null, 2),
        isError: false,
      };
    } catch (err) {
      return {
        content: `Cognee search failed: ${String(err)}`,
        isError: true,
      };
    }
  },
};

export default cogneeRecall;
