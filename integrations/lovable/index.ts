/**
 * @cognee/lovable-memory — a scoped remote MCP connector that gives the
 * Lovable builder a product memory on Cognee.
 *
 * Library use: `resolveConfig()` → `new MemoryService({config})` →
 * `createConnectorServer({config, service}).listen()`. The CLI in
 * `src/operator-cli.ts` does exactly that for `lovable-memory serve`.
 */
export { resolveConfig, validateConfig, redactConfig, type LovableMemoryConfig, type LovableMemoryConfigInput, type RecallMode } from "./src/config.js";
export { createBearerAuthenticator, generateToken, type Authenticator, type AuthDecision } from "./src/auth.js";
export { ScopePolicy } from "./src/scope.js";
export { CogneeClient, type RecallItem, type DatasetDTO, type DataItemDTO, type RememberResponse } from "./src/cognee-client.js";
export { CogneeApiError, ConfigError, describeError } from "./src/errors.js";
export { ReceiptStore, type Receipt, type ReceiptStatus } from "./src/receipts.js";
export { MemoryService, hashContent, type RecallResult, type RememberResult, type StatusResult, type ForgetResult, type HealthReport } from "./src/memory.js";
export { createMcpServer, formatRecall, TOOL_NAMES } from "./src/tools.js";
export { createConnectorServer, type ConnectorServer } from "./src/server.js";
export { createLogger, silentLogger, type Logger } from "./src/logger.js";
export { PACKAGE_NAME, PACKAGE_VERSION, VERIFIED_COGNEE_VERSION } from "./src/version.js";
