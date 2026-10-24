/**
 * Operator configuration, as the host hands it to `ctx.config.get(companyId)`.
 * The plugin never reads `process.env`; everything comes through the host.
 * Validation is explicit so `validateConfig()` can back the "Test connection"
 * button and so capture is never enabled on a half-configured instance.
 */
import { ConfigError } from "./errors.js";
import { DECISION_MARKER_DEFAULT } from "./manifest.js";

export type CaptureMode = "off" | "completed_issues" | "marked_comments" | "both";

export interface SecretRef {
  type: "secret_ref";
  secretId: string;
  version?: unknown;
}

export interface PluginConfig {
  cogneeBaseUrl: string;
  /** Either the shared secret_ref object or (test harness only) a string reference. */
  cogneeApiKeyRef: SecretRef | string;
  enabledCompanyIds: string[];
  enabledProjectIds: string[];
  companySharedDataset?: string;
  captureMode: CaptureMode;
  decisionMarker: string;
  paperclipPublicUrl?: string;
  issueLinkTemplate: string;
  instanceKey: string;
  maxRecordChars: number;
  recallTopK: number;
  maxRecallChars: number;
  recallTimeoutMs: number;
  writeTimeoutMs: number;
  maxDrainBatch: number;
}

export const DEFAULTS = {
  captureMode: "both" as CaptureMode,
  decisionMarker: DECISION_MARKER_DEFAULT,
  issueLinkTemplate: "{publicUrl}/issues/{issueId}",
  instanceKey: "paperclip",
  maxRecordChars: 6_000,
  recallTopK: 5,
  maxRecallChars: 6_000,
  recallTimeoutMs: 15_000,
  writeTimeoutMs: 30_000,
  maxDrainBatch: 50,
  statusTimeoutMs: 10_000,
  maxResponseBytes: 4 * 1024 * 1024,
};

const DATASET_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const CAPTURE_MODES: CaptureMode[] = ["off", "completed_issues", "marked_comments", "both"];

function str(raw: Record<string, unknown>, key: string): string | undefined {
  const value = raw[key];
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed;
}

function strList(raw: Record<string, unknown>, key: string): string[] {
  const value = raw[key];
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === "string" && item.trim() !== "").map((item) => item.trim());
  if (typeof value === "string") return value.split(",").map((item) => item.trim()).filter(Boolean);
  return [];
}

function int(raw: Record<string, unknown>, key: string, fallback: number, problems: string[]): number {
  const value = raw[key];
  if (value === undefined || value === null || value === "") return fallback;
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isInteger(number) || number < 0) {
    problems.push(`${key} must be a non-negative integer`);
    return fallback;
  }
  return number;
}

export function isSecretRef(value: unknown): value is SecretRef {
  return !!value && typeof value === "object" && (value as { type?: unknown }).type === "secret_ref" && typeof (value as { secretId?: unknown }).secretId === "string";
}

/** Parse + validate; throws ConfigError listing every problem at once. */
export function resolveConfig(raw: Record<string, unknown> | null | undefined): PluginConfig {
  const { config, problems } = parseConfig(raw ?? {});
  if (problems.length) throw new ConfigError(problems);
  return config;
}

export function parseConfig(raw: Record<string, unknown>): { config: PluginConfig; problems: string[]; warnings: string[] } {
  const problems: string[] = [];
  const warnings: string[] = [];
  const baseUrl = (str(raw, "cogneeBaseUrl") ?? "").replace(/\/+$/, "");
  if (!/^https?:\/\//.test(baseUrl)) problems.push("cogneeBaseUrl must be an absolute http(s) URL");

  const keyRef = raw["cogneeApiKeyRef"];
  let cogneeApiKeyRef: SecretRef | string = "";
  if (isSecretRef(keyRef)) cogneeApiKeyRef = keyRef;
  else if (typeof keyRef === "string" && keyRef.trim()) {
    cogneeApiKeyRef = keyRef.trim();
    warnings.push("cogneeApiKeyRef is a plain string; the host resolves only { type: \"secret_ref\" } bindings in production");
  } else problems.push("cogneeApiKeyRef is required: a Paperclip secret reference holding the cognee API key");

  const enabledCompanyIds = Array.from(new Set(strList(raw, "enabledCompanyIds")));
  if (!enabledCompanyIds.length) warnings.push("enabledCompanyIds is empty: no company is captured or recalled until one is listed");
  const enabledProjectIds = Array.from(new Set(strList(raw, "enabledProjectIds")));

  const companySharedDataset = str(raw, "companySharedDataset");
  if (companySharedDataset && !DATASET_NAME.test(companySharedDataset)) problems.push("companySharedDataset is not a valid cognee dataset name");

  const captureModeRaw = str(raw, "captureMode") ?? DEFAULTS.captureMode;
  if (!CAPTURE_MODES.includes(captureModeRaw as CaptureMode)) problems.push(`captureMode must be one of ${CAPTURE_MODES.join(", ")}`);
  const captureMode = (CAPTURE_MODES.includes(captureModeRaw as CaptureMode) ? captureModeRaw : DEFAULTS.captureMode) as CaptureMode;
  if (captureMode !== "off" && !enabledProjectIds.length) warnings.push("captureMode is on but enabledProjectIds is empty: nothing will be captured automatically");

  const decisionMarker = str(raw, "decisionMarker") ?? DEFAULTS.decisionMarker;
  if (/\s/.test(decisionMarker)) problems.push("decisionMarker must not contain whitespace");

  const paperclipPublicUrl = str(raw, "paperclipPublicUrl")?.replace(/\/+$/, "");
  if (paperclipPublicUrl && !/^https?:\/\//.test(paperclipPublicUrl)) problems.push("paperclipPublicUrl must be an absolute http(s) URL");
  if (!paperclipPublicUrl) warnings.push("paperclipPublicUrl is not set: stored records carry issue ids but no clickable links");

  const config: PluginConfig = {
    cogneeBaseUrl: baseUrl,
    cogneeApiKeyRef,
    enabledCompanyIds,
    enabledProjectIds,
    companySharedDataset,
    captureMode,
    decisionMarker,
    paperclipPublicUrl,
    issueLinkTemplate: str(raw, "issueLinkTemplate") ?? DEFAULTS.issueLinkTemplate,
    instanceKey: str(raw, "instanceKey") ?? DEFAULTS.instanceKey,
    maxRecordChars: int(raw, "maxRecordChars", DEFAULTS.maxRecordChars, problems),
    recallTopK: int(raw, "recallTopK", DEFAULTS.recallTopK, problems),
    maxRecallChars: int(raw, "maxRecallChars", DEFAULTS.maxRecallChars, problems),
    recallTimeoutMs: int(raw, "recallTimeoutMs", DEFAULTS.recallTimeoutMs, problems),
    writeTimeoutMs: int(raw, "writeTimeoutMs", DEFAULTS.writeTimeoutMs, problems),
    maxDrainBatch: int(raw, "maxDrainBatch", DEFAULTS.maxDrainBatch, problems),
  };
  if (config.recallTopK < 1 || config.recallTopK > 20) problems.push("recallTopK must be 1-20");
  if (config.maxRecordChars < 500) problems.push("maxRecordChars must be at least 500");
  return { config, problems, warnings };
}

/** Shape expected by `onValidateConfig`. */
export function validateConfig(raw: Record<string, unknown> | null | undefined): { ok: boolean; errors?: string[]; warnings?: string[] } {
  const { problems, warnings } = parseConfig(raw ?? {});
  return { ok: problems.length === 0, errors: problems.length ? problems : undefined, warnings: warnings.length ? warnings : undefined };
}

/** A copy safe to log: the secret reference is replaced by a marker. */
export function redactConfig(config: PluginConfig): Record<string, unknown> {
  return { ...config, cogneeApiKeyRef: config.cogneeApiKeyRef ? "<secret_ref>" : "<missing>" };
}
