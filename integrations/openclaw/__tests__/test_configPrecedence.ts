import { resolveConfig } from "../src/config";
import type { CogneePluginConfig } from "../src/types";

const ENV_KEYS = [
  "COGNEE_MODE",
  "COGNEE_BASE_URL",
  "COGNEE_API_KEY",
  "COGNEE_USERNAME",
  "COGNEE_PASSWORD",
  "COGNEE_OPENCLAW_DATASET_NAME",
  "COGNEE_OPENCLAW_COMPANY_DATASET",
  "COGNEE_OPENCLAW_USER_DATASET_PREFIX",
  "COGNEE_OPENCLAW_AGENT_DATASET_PREFIX",
  "COGNEE_OPENCLAW_AGENT_DATASET_TEMPLATE",
  "OPENCLAW_USER_ID",
  "OPENCLAW_AGENT_ID",
  "COGNEE_OPENCLAW_RECALL_SCOPES",
  "COGNEE_OPENCLAW_DEFAULT_WRITE_SCOPE",
  "COGNEE_OPENCLAW_SCOPE_ROUTING",
  "COGNEE_OPENCLAW_PER_AGENT_MEMORY",
  "COGNEE_OPENCLAW_RECALL_INJECTION_POSITION",
  "COGNEE_OPENCLAW_ENABLE_SESSIONS",
  "COGNEE_OPENCLAW_PERSIST_SESSIONS_AFTER_END",
  "COGNEE_OPENCLAW_SEARCH_TYPE",
  "COGNEE_OPENCLAW_SEARCH_PROMPT",
  "COGNEE_OPENCLAW_DELETE_MODE",
  "COGNEE_OPENCLAW_MAX_RESULTS",
  "COGNEE_OPENCLAW_MIN_SCORE",
  "COGNEE_OPENCLAW_MAX_TOKENS",
  "COGNEE_OPENCLAW_AUTO_RECALL",
  "COGNEE_OPENCLAW_AUTO_INDEX",
  "COGNEE_OPENCLAW_AUTO_COGNIFY",
  "COGNEE_OPENCLAW_AUTO_MEMIFY",
  "COGNEE_OPENCLAW_IMPROVE_ON_SESSION_END",
  "COGNEE_OPENCLAW_REQUEST_TIMEOUT_MS",
  "COGNEE_OPENCLAW_INGESTION_TIMEOUT_MS",
];

function clearEnv() {
  for (const key of ENV_KEYS) {
    delete process.env[key];
  }
}

describe("resolveConfig precedence", () => {
  beforeEach(clearEnv);
  afterEach(clearEnv);

  it("uses defaults when neither env nor config is set", () => {
    expect(resolveConfig({})).toMatchObject({
      mode: "local",
      baseUrl: "http://localhost:8000",
      datasetName: "openclaw",
      searchType: "GRAPH_COMPLETION",
      deleteMode: "soft",
      maxResults: 3,
      minScore: 0.3,
      maxTokens: 512,
      recallScopes: ["agent", "user", "company"],
      defaultWriteScope: "agent",
      agentId: "default",
      autoRecall: true,
      autoIndex: true,
      autoCognify: true,
      autoMemify: false,
      improveOnSessionEnd: true,
      requestTimeoutMs: 60000,
      ingestionTimeoutMs: 300000,
    });
  });

  it("uses config values over defaults", () => {
    const raw: CogneePluginConfig = {
      mode: "cloud",
      baseUrl: "https://file.example",
      apiKey: "file-key",
      username: "file-user",
      password: "file-pass",
      datasetName: "file-dataset",
      companyDataset: "file-company",
      userDatasetPrefix: "file-user-prefix",
      agentDatasetPrefix: "file-agent-prefix",
      agentDatasetTemplate: "file-{agentId}",
      userId: "file-user-id",
      agentId: "file-agent-id",
      recallScopes: ["company", "user"],
      defaultWriteScope: "user",
      scopeRouting: [{ pattern: "file/**", scope: "company" }],
      perAgentMemory: true,
      recallInjectionPosition: "appendSystemContext",
      enableSessions: false,
      persistSessionsAfterEnd: false,
      searchType: "CHUNKS",
      searchPrompt: "file prompt",
      deleteMode: "hard",
      maxResults: 7,
      minScore: 0.7,
      maxTokens: 700,
      autoRecall: false,
      autoIndex: false,
      autoCognify: false,
      autoMemify: true,
      improveOnSessionEnd: false,
      requestTimeoutMs: 7000,
      ingestionTimeoutMs: 17000,
    };

    expect(resolveConfig(raw)).toMatchObject(raw);
  });

  it("uses env values over config values for every env-overridable setting", () => {
    const raw: CogneePluginConfig = {
      mode: "local",
      baseUrl: "https://file.example",
      apiKey: "file-key",
      username: "file-user",
      password: "file-pass",
      datasetName: "file-dataset",
      companyDataset: "file-company",
      userDatasetPrefix: "file-user-prefix",
      agentDatasetPrefix: "file-agent-prefix",
      agentDatasetTemplate: "file-{agentId}",
      userId: "file-user-id",
      agentId: "file-agent-id",
      recallScopes: ["company"],
      defaultWriteScope: "company",
      scopeRouting: [{ pattern: "file/**", scope: "company" }],
      perAgentMemory: false,
      recallInjectionPosition: "appendSystemContext",
      enableSessions: false,
      persistSessionsAfterEnd: false,
      searchType: "CHUNKS",
      searchPrompt: "file prompt",
      deleteMode: "soft",
      maxResults: 7,
      minScore: 0.7,
      maxTokens: 700,
      autoRecall: false,
      autoIndex: false,
      autoCognify: false,
      autoMemify: false,
      improveOnSessionEnd: false,
      requestTimeoutMs: 7000,
      ingestionTimeoutMs: 17000,
    };

    process.env.COGNEE_MODE = "cloud";
    process.env.COGNEE_BASE_URL = "https://env.example";
    process.env.COGNEE_API_KEY = "env-key";
    process.env.COGNEE_USERNAME = "env-user";
    process.env.COGNEE_PASSWORD = "env-pass";
    process.env.COGNEE_OPENCLAW_DATASET_NAME = "env-dataset";
    process.env.COGNEE_OPENCLAW_COMPANY_DATASET = "env-company";
    process.env.COGNEE_OPENCLAW_USER_DATASET_PREFIX = "env-user-prefix";
    process.env.COGNEE_OPENCLAW_AGENT_DATASET_PREFIX = "env-agent-prefix";
    process.env.COGNEE_OPENCLAW_AGENT_DATASET_TEMPLATE = "env-{agentId}";
    process.env.OPENCLAW_USER_ID = "env-user-id";
    process.env.OPENCLAW_AGENT_ID = "env-agent-id";
    process.env.COGNEE_OPENCLAW_RECALL_SCOPES = "agent,user";
    process.env.COGNEE_OPENCLAW_DEFAULT_WRITE_SCOPE = "agent";
    process.env.COGNEE_OPENCLAW_SCOPE_ROUTING = JSON.stringify([{ pattern: "env/**", scope: "user" }]);
    process.env.COGNEE_OPENCLAW_PER_AGENT_MEMORY = "true";
    process.env.COGNEE_OPENCLAW_RECALL_INJECTION_POSITION = "prependContext";
    process.env.COGNEE_OPENCLAW_ENABLE_SESSIONS = "true";
    process.env.COGNEE_OPENCLAW_PERSIST_SESSIONS_AFTER_END = "true";
    process.env.COGNEE_OPENCLAW_SEARCH_TYPE = "RAG_COMPLETION";
    process.env.COGNEE_OPENCLAW_SEARCH_PROMPT = "env prompt";
    process.env.COGNEE_OPENCLAW_DELETE_MODE = "hard";
    process.env.COGNEE_OPENCLAW_MAX_RESULTS = "9";
    process.env.COGNEE_OPENCLAW_MIN_SCORE = "0.9";
    process.env.COGNEE_OPENCLAW_MAX_TOKENS = "900";
    process.env.COGNEE_OPENCLAW_AUTO_RECALL = "true";
    process.env.COGNEE_OPENCLAW_AUTO_INDEX = "true";
    process.env.COGNEE_OPENCLAW_AUTO_COGNIFY = "true";
    process.env.COGNEE_OPENCLAW_AUTO_MEMIFY = "true";
    process.env.COGNEE_OPENCLAW_IMPROVE_ON_SESSION_END = "true";
    process.env.COGNEE_OPENCLAW_REQUEST_TIMEOUT_MS = "9000";
    process.env.COGNEE_OPENCLAW_INGESTION_TIMEOUT_MS = "19000";

    expect(resolveConfig(raw)).toMatchObject({
      mode: "cloud",
      baseUrl: "https://env.example",
      apiKey: "env-key",
      username: "env-user",
      password: "env-pass",
      datasetName: "env-dataset",
      companyDataset: "env-company",
      userDatasetPrefix: "env-user-prefix",
      agentDatasetPrefix: "env-agent-prefix",
      agentDatasetTemplate: "env-{agentId}",
      userId: "env-user-id",
      agentId: "env-agent-id",
      recallScopes: ["agent", "user"],
      defaultWriteScope: "agent",
      scopeRouting: [{ pattern: "env/**", scope: "user" }],
      perAgentMemory: true,
      recallInjectionPosition: "prependContext",
      enableSessions: true,
      persistSessionsAfterEnd: true,
      searchType: "RAG_COMPLETION",
      searchPrompt: "env prompt",
      deleteMode: "hard",
      maxResults: 9,
      minScore: 0.9,
      maxTokens: 900,
      autoRecall: true,
      autoIndex: true,
      autoCognify: true,
      autoMemify: true,
      improveOnSessionEnd: true,
      requestTimeoutMs: 9000,
      ingestionTimeoutMs: 19000,
    });
  });
});
