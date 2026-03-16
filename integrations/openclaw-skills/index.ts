import { promises as fs } from "node:fs";
import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
// Fix #2: Import the shared HTTP client from the memory plugin package
import { CogneeHttpClient } from "@cognee/cognee-openclaw";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type SkillsPluginConfig = {
  baseUrl?: string;
  apiKey?: string;
  username?: string;
  password?: string;
  skillsFolder?: string;
  datasetName?: string;
  autoIngest?: boolean;
  autoObserve?: boolean;
  autoAmendify?: boolean;
  amendifyMinRuns?: number;
  amendifyScoreThreshold?: number;
  requestTimeoutMs?: number;
  ingestionTimeoutMs?: number;
};

type SkillSummary = {
  skill_id: string;
  name: string;
  instruction_summary: string;
  tags: string[];
  complexity: string;
};

type SkillDetail = SkillSummary & {
  instructions: string;
  description: string;
  source_path: string;
  task_patterns: { pattern_key: string; text: string; category: string }[];
};

type ExecuteResult = {
  output: string;
  skill_id: string;
  model: string;
  latency_ms: number;
  success: boolean;
  error?: string;
  quality_score?: number;
  quality_reason?: string;
  amended?: unknown;
};

type InspectionResult = {
  inspection_id: string;
  skill_id: string;
  skill_name: string;
  failure_category: string;
  root_cause: string;
  severity: string;
  improvement_hypothesis: string;
  analyzed_run_count: number;
  avg_success_score: number;
  inspection_confidence: number;
};

type AmendmentPreview = {
  amendment_id: string;
  skill_id: string;
  skill_name: string;
  inspection_id: string;
  original_instructions: string;
  amended_instructions: string;
  change_explanation: string;
  expected_improvement: string;
  status: string;
  amendment_confidence: number;
  pre_amendment_avg_score: number;
};

type SyncState = {
  lastIngestHash?: string;
  skillCount?: number;
};

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

const DEFAULT_BASE_URL = "http://localhost:8000";
const DEFAULT_SKILLS_FOLDER = "skills";
const DEFAULT_DATASET_NAME = "skills";
const DEFAULT_AUTO_INGEST = true;
const DEFAULT_AUTO_OBSERVE = true;
const DEFAULT_AUTO_AMENDIFY = false;
const DEFAULT_AMENDIFY_MIN_RUNS = 3;
const DEFAULT_AMENDIFY_SCORE_THRESHOLD = 0.5;
const DEFAULT_REQUEST_TIMEOUT_MS = 60_000;
const DEFAULT_INGESTION_TIMEOUT_MS = 300_000;

const STATE_PATH = join(homedir(), ".openclaw", "skills", "cognee", "state.json");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function resolveEnvVars(value: string): string {
  return value.replace(/\$\{([^}]+)\}/g, (_, envVar) => {
    const envValue = process.env[envVar];
    if (!envValue) {
      throw new Error(`Environment variable ${envVar} is not set`);
    }
    return envValue;
  });
}

function resolveConfig(rawConfig: unknown): Required<SkillsPluginConfig> {
  const raw =
    rawConfig && typeof rawConfig === "object" && !Array.isArray(rawConfig)
      ? (rawConfig as SkillsPluginConfig)
      : {};

  const apiKey =
    raw.apiKey && raw.apiKey.length > 0
      ? resolveEnvVars(raw.apiKey)
      : process.env.COGNEE_API_KEY || "";

  return {
    baseUrl: raw.baseUrl?.trim() || DEFAULT_BASE_URL,
    apiKey,
    username: raw.username?.trim() || process.env.COGNEE_USERNAME || "",
    password: raw.password?.trim() || process.env.COGNEE_PASSWORD || "",
    skillsFolder: raw.skillsFolder?.trim() || DEFAULT_SKILLS_FOLDER,
    datasetName: raw.datasetName?.trim() || DEFAULT_DATASET_NAME,
    autoIngest: typeof raw.autoIngest === "boolean" ? raw.autoIngest : DEFAULT_AUTO_INGEST,
    autoObserve: typeof raw.autoObserve === "boolean" ? raw.autoObserve : DEFAULT_AUTO_OBSERVE,
    autoAmendify: typeof raw.autoAmendify === "boolean" ? raw.autoAmendify : DEFAULT_AUTO_AMENDIFY,
    amendifyMinRuns: typeof raw.amendifyMinRuns === "number" ? raw.amendifyMinRuns : DEFAULT_AMENDIFY_MIN_RUNS,
    amendifyScoreThreshold: typeof raw.amendifyScoreThreshold === "number" ? raw.amendifyScoreThreshold : DEFAULT_AMENDIFY_SCORE_THRESHOLD,
    requestTimeoutMs: typeof raw.requestTimeoutMs === "number" ? raw.requestTimeoutMs : DEFAULT_REQUEST_TIMEOUT_MS,
    ingestionTimeoutMs: typeof raw.ingestionTimeoutMs === "number" ? raw.ingestionTimeoutMs : DEFAULT_INGESTION_TIMEOUT_MS,
  };
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

async function loadState(): Promise<SyncState> {
  try {
    const raw = await fs.readFile(STATE_PATH, "utf-8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    return parsed as SyncState;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
}

async function saveState(state: SyncState): Promise<void> {
  await fs.mkdir(dirname(STATE_PATH), { recursive: true });
  await fs.writeFile(STATE_PATH, JSON.stringify(state, null, 2), "utf-8");
}

// ---------------------------------------------------------------------------
// Skill file scanning
// ---------------------------------------------------------------------------

async function hashSkillsFolder(skillsDir: string): Promise<{ hash: string; fileCount: number }> {
  const hasher = createHash("sha256");
  let fileCount = 0;

  try {
    const entries = await fs.readdir(skillsDir, { withFileTypes: true });
    for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      if (!entry.isDirectory()) continue;
      const skillMd = join(skillsDir, entry.name, "SKILL.md");
      try {
        const content = await fs.readFile(skillMd, "utf-8");
        hasher.update(`${entry.name}:${content}`);
        fileCount++;
      } catch {
        // No SKILL.md in this subdirectory
      }
    }
  } catch {
    // Directory doesn't exist
  }

  return { hash: hasher.digest("hex"), fileCount };
}

// ---------------------------------------------------------------------------
// CogneeSkillsClient — uses shared CogneeHttpClient for transport
//
// Fix #2: All HTTP auth/retry/fetch logic is now in CogneeHttpClient.
// This class only adds skills-specific endpoint methods.
// ---------------------------------------------------------------------------

class CogneeSkillsClient {
  private http: CogneeHttpClient;

  constructor(
    baseUrl: string,
    apiKey?: string,
    username?: string,
    password?: string,
    timeoutMs: number = DEFAULT_REQUEST_TIMEOUT_MS,
    ingestionTimeoutMs: number = DEFAULT_INGESTION_TIMEOUT_MS,
  ) {
    this.http = new CogneeHttpClient(baseUrl, apiKey, username, password, timeoutMs, ingestionTimeoutMs);
  }

  async ingest(skillsFolder: string, datasetName: string): Promise<void> {
    await this.http.fetchJson("/api/v1/skills/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skills_folder: skillsFolder, dataset_name: datasetName }),
    }, this.http.ingestionTimeoutMs);
  }

  async upsert(skillsFolder: string, datasetName: string): Promise<unknown> {
    return this.http.fetchJson("/api/v1/skills/upsert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skills_folder: skillsFolder, dataset_name: datasetName }),
    }, this.http.ingestionTimeoutMs);
  }

  async list(): Promise<SkillSummary[]> {
    return this.http.fetchJson<SkillSummary[]>("/api/v1/skills", { method: "GET" });
  }

  async load(skillId: string): Promise<SkillDetail | null> {
    try {
      return await this.http.fetchJson<SkillDetail>(`/api/v1/skills/${encodeURIComponent(skillId)}`, { method: "GET" });
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      if (msg.includes("404")) return null;
      throw error;
    }
  }

  async execute(params: {
    skillId: string;
    taskText: string;
    context?: string;
    autoObserve?: boolean;
    autoEvaluate?: boolean;
    autoAmendify?: boolean;
    amendifyMinRuns?: number;
    amendifyScoreThreshold?: number;
    sessionId?: string;
  }): Promise<ExecuteResult> {
    return this.http.fetchJson<ExecuteResult>("/api/v1/skills/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        skill_id: params.skillId,
        task_text: params.taskText,
        context: params.context || null,
        auto_observe: params.autoObserve ?? true,
        auto_evaluate: params.autoEvaluate ?? true,
        auto_amendify: params.autoAmendify ?? false,
        amendify_min_runs: params.amendifyMinRuns ?? 3,
        amendify_score_threshold: params.amendifyScoreThreshold ?? 0.5,
        session_id: params.sessionId ?? "default",
      }),
    });
  }

  async observe(params: {
    taskText: string;
    selectedSkillId: string;
    successScore: number;
    sessionId?: string;
    resultSummary?: string;
    errorType?: string;
    errorMessage?: string;
    latencyMs?: number;
  }): Promise<unknown> {
    return this.http.fetchJson("/api/v1/skills/observe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_text: params.taskText,
        selected_skill_id: params.selectedSkillId,
        success_score: params.successScore,
        session_id: params.sessionId ?? "default",
        result_summary: params.resultSummary ?? "",
        error_type: params.errorType ?? "",
        error_message: params.errorMessage ?? "",
        latency_ms: params.latencyMs ?? 0,
      }),
    });
  }

  async inspect(skillId: string, minRuns = 1, scoreThreshold = 0.5): Promise<InspectionResult | null> {
    const result = await this.http.fetchJson<InspectionResult | { result: null; message: string }>("/api/v1/skills/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill_id: skillId, min_runs: minRuns, score_threshold: scoreThreshold }),
    });
    if ("result" in result && result.result === null) return null;
    return result as InspectionResult;
  }

  async previewAmendify(skillId: string, inspectionId?: string, minRuns = 1, scoreThreshold = 0.5): Promise<AmendmentPreview | null> {
    const result = await this.http.fetchJson<AmendmentPreview | { result: null; message: string }>("/api/v1/skills/preview-amendify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill_id: skillId, inspection_id: inspectionId || null, min_runs: minRuns, score_threshold: scoreThreshold }),
    });
    if ("result" in result && result.result === null) return null;
    return result as AmendmentPreview;
  }

  async amendify(amendmentId: string): Promise<unknown> {
    return this.http.fetchJson("/api/v1/skills/amendify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amendment_id: amendmentId }),
    });
  }

  async rollback(amendmentId: string): Promise<{ success: boolean }> {
    return this.http.fetchJson<{ success: boolean }>("/api/v1/skills/rollback-amendify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amendment_id: amendmentId }),
    });
  }

  async evaluateAmendify(amendmentId: string): Promise<{ pre_avg: number; post_avg: number; improvement: number; recommendation: string }> {
    return this.http.fetchJson("/api/v1/skills/evaluate-amendify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amendment_id: amendmentId }),
    });
  }

  async autoAmendify(skillId: string, minRuns = 3, scoreThreshold = 0.5): Promise<unknown | null> {
    const result = await this.http.fetchJson<unknown>("/api/v1/skills/auto-amendify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill_id: skillId, min_runs: minRuns, score_threshold: scoreThreshold }),
    });
    if (result && typeof result === "object" && "result" in result && (result as Record<string, unknown>).result === null) {
      return null;
    }
    return result;
  }
}

// ---------------------------------------------------------------------------
// Plugin registration
// ---------------------------------------------------------------------------

const skillsCogneePlugin = {
  id: "cognee-openclaw-skills",
  name: "Skills (Cognee)",
  description: "Self-improving agent skills: ingest SKILL.md files, execute against tasks, and automatically fix failing skills over time",
  kind: "skills" as const,
  register(api: OpenClawPluginApi) {
    const cfg = resolveConfig(api.pluginConfig);
    const client = new CogneeSkillsClient(cfg.baseUrl, cfg.apiKey, cfg.username, cfg.password, cfg.requestTimeoutMs, cfg.ingestionTimeoutMs);

    let resolvedWorkspaceDir: string | undefined;

    api.registerCli((ctx) => {
      const skills = ctx.program.command("cognee-skills").description("Cognee self-improving skills management");
      const workspaceDir = ctx.workspaceDir || process.cwd();

      skills.command("ingest").description("Ingest SKILL.md files from the skills folder").action(async () => {
        const folder = resolve(workspaceDir, cfg.skillsFolder);
        ctx.logger.info?.(`cognee-skills: ingesting from ${folder}`);
        await client.upsert(folder, cfg.datasetName);
        const list = await client.list();
        console.log(`Ingested ${list.length} skill(s) from ${folder}`);
      });

      skills.command("list").description("List all ingested skills").action(async () => {
        const list = await client.list();
        if (list.length === 0) { console.log("No skills ingested yet."); return; }
        for (const s of list) {
          const tags = s.tags.length > 0 ? ` [${s.tags.join(", ")}]` : "";
          console.log(`  ${s.skill_id} — ${s.name}${tags}`);
        }
        console.log(`\n${list.length} skill(s) total`);
      });

      skills.command("inspect").argument("<skill_id>", "Skill ID to inspect").description("Analyze failed runs").action(async (skillId: string) => {
        const result = await client.inspect(skillId, cfg.amendifyMinRuns, cfg.amendifyScoreThreshold);
        if (!result) { console.log(`No issues found for '${skillId}'.`); return; }
        console.log(JSON.stringify(result, null, 2));
      });

      skills.command("preview").argument("<skill_id>", "Skill ID").description("Preview proposed fix").action(async (skillId: string) => {
        const result = await client.previewAmendify(skillId, undefined, cfg.amendifyMinRuns, cfg.amendifyScoreThreshold);
        if (!result) { console.log(`No amendment proposed for '${skillId}'.`); return; }
        console.log(JSON.stringify(result, null, 2));
      });

      skills.command("amendify").argument("<amendment_id>", "Amendment ID").description("Apply amendment").action(async (amendmentId: string) => {
        console.log(JSON.stringify(await client.amendify(amendmentId), null, 2));
      });

      skills.command("rollback").argument("<amendment_id>", "Amendment ID").description("Revert amendment").action(async (amendmentId: string) => {
        const result = await client.rollback(amendmentId);
        console.log(result.success ? `Rolled back '${amendmentId}'.` : `Rollback failed for '${amendmentId}'.`);
      });

      skills.command("evaluate").argument("<amendment_id>", "Amendment ID").description("Compare pre/post scores").action(async (amendmentId: string) => {
        console.log(JSON.stringify(await client.evaluateAmendify(amendmentId), null, 2));
      });

      skills.command("auto-fix").argument("<skill_id>", "Skill ID").description("One-call self-improvement").action(async (skillId: string) => {
        const result = await client.autoAmendify(skillId, cfg.amendifyMinRuns, cfg.amendifyScoreThreshold);
        if (!result) { console.log(`No fix needed for '${skillId}'.`); return; }
        console.log(JSON.stringify(result, null, 2));
      });
    }, { commands: ["cognee-skills"] });

    if (cfg.autoIngest) {
      api.registerService({
        id: "cognee-skills-auto-ingest",
        async start(ctx) {
          resolvedWorkspaceDir = ctx.workspaceDir || process.cwd();
          const skillsDir = resolve(resolvedWorkspaceDir, cfg.skillsFolder);
          try {
            const { hash, fileCount } = await hashSkillsFolder(skillsDir);
            if (fileCount === 0) { ctx.logger.info?.(`cognee-skills: no SKILL.md files found in ${skillsDir}`); return; }
            const state = await loadState();
            if (state.lastIngestHash === hash) { ctx.logger.info?.(`cognee-skills: ${fileCount} skill(s) unchanged`); return; }
            ctx.logger.info?.(`cognee-skills: ingesting ${fileCount} skill(s) from ${skillsDir}`);
            await client.upsert(skillsDir, cfg.datasetName);
            await saveState({ lastIngestHash: hash, skillCount: fileCount });
            ctx.logger.info?.(`cognee-skills: auto-ingest complete (${fileCount} skills)`);
          } catch (error) {
            ctx.logger.warn?.(`cognee-skills: auto-ingest failed: ${String(error)}`);
          }
        },
      });
    }

    if (cfg.autoObserve) {
      api.on("agent_end", async (event, ctx) => {
        if (!event.success && !event.error) return;
        const skillId = (event as Record<string, unknown>).skill_id as string | undefined;
        const taskText = (event as Record<string, unknown>).prompt as string | undefined;
        if (!skillId || !taskText) return;
        const successScore = event.success ? 1.0 : 0.0;
        try {
          await client.observe({
            taskText, selectedSkillId: skillId, successScore,
            sessionId: (ctx.sessionKey as string) ?? "default",
            resultSummary: typeof (event as Record<string, unknown>).output === "string" ? ((event as Record<string, unknown>).output as string).slice(0, 500) : "",
            errorType: event.error ? "agent_error" : "",
            errorMessage: event.error ? String(event.error) : "",
          });
          api.logger.info?.(`cognee-skills: observed '${skillId}' (score: ${successScore})`);
          if (cfg.autoAmendify && !event.success) {
            try {
              const result = await client.autoAmendify(skillId, cfg.amendifyMinRuns, cfg.amendifyScoreThreshold);
              if (result) api.logger.info?.(`cognee-skills: auto-amendify applied for '${skillId}'`);
            } catch (error) {
              api.logger.warn?.(`cognee-skills: auto-amendify failed for '${skillId}': ${String(error)}`);
            }
          }
        } catch (error) {
          api.logger.warn?.(`cognee-skills: observe failed: ${String(error)}`);
        }
      });
    }

    if (cfg.autoIngest) {
      api.on("agent_end", async (event) => {
        if (!event.success) return;
        const workspaceDir = resolvedWorkspaceDir || process.cwd();
        const skillsDir = resolve(workspaceDir, cfg.skillsFolder);
        try {
          const { hash, fileCount } = await hashSkillsFolder(skillsDir);
          if (fileCount === 0) return;
          const state = await loadState();
          if (state.lastIngestHash === hash) return;
          api.logger.info?.(`cognee-skills: skills folder changed, re-ingesting ${fileCount} skill(s)`);
          await client.upsert(skillsDir, cfg.datasetName);
          await saveState({ lastIngestHash: hash, skillCount: fileCount });
        } catch (error) {
          api.logger.warn?.(`cognee-skills: post-agent re-ingest failed: ${String(error)}`);
        }
      });
    }
  },
};

export default skillsCogneePlugin;
export { CogneeSkillsClient };
export type { SkillsPluginConfig, SkillSummary, SkillDetail, ExecuteResult, InspectionResult, AmendmentPreview, SyncState };
