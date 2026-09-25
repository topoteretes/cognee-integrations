/**
 * `@cognee/cognee-mastra` — public barrel. Re-exports `createCogneeProcessors`,
 * `withCognee`, `createCogneeTools`, the four per-tool factories,
 * `CogneeClient` (the escape hatch), `resolveConfig`, `VERSION`, and every
 * public type these surfaces take or return. Not re-exported:
 * `src/capabilities.ts` and `src/scope.ts`/`src/format.ts`'s helpers —
 * internal implementation details of `CogneeClient`/the processors/the tools.
 */

export { VERSION } from "./src/version.js";
export { CogneeApiError, isRetryable } from "./src/errors.js";
export type {
  SearchType,
  ContextFormat,
  CogneeMastraConfig,
  CogneeRecallConfig,
  CogneeWriteConfig,
  CogneeToolsConfig,
  CogneeAuthConfig,
  HealthResponse,
  LoginRequest,
  LoginResponse,
  DatasetDTO,
  EnsureDatasetRequest,
  RecallRequest,
  RecallResponseItem,
  RecallResponse,
  SearchResponseItem,
  SearchResponse,
  RememberRequest,
  AddRequest,
  CognifyRequest,
  QAEntryDTO,
  RememberEntryRequest,
  RememberResult,
  ForgetRequest,
  PipelineRunStatusValue,
  DatasetStatusResponse,
  ImproveRequest,
  RecallHit,
} from "./src/types.js";

// -- Client (escape hatch) ---------------------------------------------------

export { CogneeClient } from "./src/client.js";
export type { RequestOptions } from "./src/client.js";

// -- Config (resolveConfig) --------------------------------------------------

export { resolveConfig, redactConfigForLogging } from "./src/config.js";
export type {
  ResolvedCogneeConfig,
  ResolvedCogneeRecallConfig,
  ResolvedCogneeWriteConfig,
  ResolvedCogneeToolsConfig,
} from "./src/config.js";

// -- Circuit breaker (escape hatch for CogneeProcessorsConfig.breaker) ------

export { CircuitBreaker, DEFAULT_BREAKER_THRESHOLD, DEFAULT_BREAKER_COOLDOWN_MS } from "./src/breaker.js";
export type { CircuitBreakerOptions } from "./src/breaker.js";

// -- Processors ---------------------------------------------------------------

export { CogneeInputProcessor, CogneeOutputProcessor, createCogneeProcessors } from "./src/processors.js";
export type { CogneeProcessorOptions, CogneeProcessors, CogneeProcessorsConfig } from "./src/processors.js";

// -- withCognee ----------------------------------------------------------------

export { withCognee } from "./src/with-cognee.js";

// -- Tools -----------------------------------------------------------------------

export {
  createCogneeSearchTool,
  createCogneeAskTool,
  createCogneeRememberTool,
  createCogneeForgetTool,
  createCogneeTools,
} from "./src/tools.js";
export type { CogneeToolFactoryOptions, CogneeToolsFactoryConfig } from "./src/tools.js";
