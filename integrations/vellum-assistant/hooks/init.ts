/**
 * init hook — fires once when the plugin is registered (on boot or install).
 *
 * Responsibilities:
 *   1. Disable Vellum's default memory system (config.json + .disabled sentinels)
 *   2. Resolve the Cognee backend (local or cloud)
 *   3. Mint/resolve an API key if needed
 *   4. Ensure the dataset exists
 *   5. Register the session as an active agent connection
 *   6. Start the idle watcher + exit watcher (background)
 *   7. Inject a system message telling the assistant Cognee memory is active
 */

import type { InitContext } from "@vellumai/plugin-api";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import {
  validateConfig,
  saveConfig,
  hookLog,
  markServerReady,
  cacheApiKey,
  resolveApiKey,
  isLocalUrl,
  touchActivity,
} from "../src/plugin-common.ts";
import {
  backendReachable,
  ensureDataset,
  checkLlmKey,
} from "../src/cognee-client.ts";
import { ensureLocalServer } from "../src/managed-server.ts";
import { getPluginRoot } from "../src/bridge.ts";

// ─── Vellum default memory disabling ──────────────────────────────────────────

/**
 * Write memory.enabled=false and memory.v2.enabled=false to the workspace
 * config.json. The daemon's config cache auto-invalidates on file change,
 * so the next getConfig() picks up the edit.
 *
 * Derives the workspace dir from ctx.pluginStorageDir
 * (<workspace>/plugins-data/<plugin>/ → up two levels).
 */
function disableMemoryInConfig(pluginStorageDir: string): void {
  try {
    const workspace = join(pluginStorageDir, "..", "..");
    const configPath = join(workspace, "config.json");

    // Read existing config, merge our overrides.
    let config: Record<string, unknown> = {};
    if (existsSync(configPath)) {
      try {
        config = JSON.parse(readFileSync(configPath, "utf-8"));
      } catch {
        // Corrupt or empty — start fresh.
      }
    }

    // Set memory.enabled = false
    if (!config.memory || typeof config.memory !== "object") {
      config.memory = {};
    }
    (config.memory as Record<string, unknown>).enabled = false;

    // Set memory.v2.enabled = false
    const mem = config.memory as Record<string, unknown>;
    if (!mem.v2 || typeof mem.v2 !== "object") {
      mem.v2 = {};
    }
    (mem.v2 as Record<string, unknown>).enabled = false;

    writeFileSync(configPath, JSON.stringify(config, null, 2), "utf-8");
    hookLog("memory_disabled_in_config", { path: configPath });
  } catch (err) {
    hookLog("memory_disable_config_failed", { error: String(err).slice(0, 200) });
  }
}

/**
 * Create .disabled sentinel files for the memory-retrieval and memory-v3-shadow
 * default plugins. This prevents them from being bootstrapped.
 *
 * The sentinel files go at <workspace>/plugins/<manifest-name>/.disabled.
 */
function disableDefaultMemoryPlugins(pluginStorageDir: string): void {
  const workspace = join(pluginStorageDir, "..", "..");
  const pluginsDir = join(workspace, "plugins");

  const defaultMemoryPlugins = [
    "default-memory-retrieval",
    "default-memory-v3-shadow",
  ];

  for (const name of defaultMemoryPlugins) {
    try {
      const pluginDir = join(pluginsDir, name);
      const sentinelPath = join(pluginDir, ".disabled");
      if (!existsSync(sentinelPath)) {
        mkdirSync(pluginDir, { recursive: true });
        writeFileSync(sentinelPath, "", "utf-8");
        hookLog("default_memory_plugin_disabled", { plugin: name });
      }
    } catch (err) {
      hookLog("default_memory_plugin_disable_failed", {
        plugin: name,
        error: String(err).slice(0, 200),
      });
    }
  }
}

// ─── API key minting (local mode) ─────────────────────────────────────────────

/**
 * Mint an API key from the local Cognee server's default user.
 * Only used when no key is in env or cache and the server is local.
 */
async function mintApiKey(baseUrl: string): Promise<string> {
  try {
    // 1. Login as default user
    const loginResp = await fetch(`${baseUrl.replace(/\/+$/, "")}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "username=&password=",
    });
    if (!loginResp.ok) return "";

    // 2. Create an API key
    const keyResp = await fetch(`${baseUrl.replace(/\/+$/, "")}/api/v1/auth/api-keys`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (!keyResp.ok) return "";

    const data = await keyResp.json() as Record<string, unknown>;
    const key = String(data.api_key ?? data.key ?? "");
    if (key) {
      cacheApiKey(key, baseUrl);
      process.env.COGNEE_API_KEY = key;
    }
    return key;
  } catch {
    return "";
  }
}

// ─── Init hook ────────────────────────────────────────────────────────────────

export default async function init(ctx: InitContext): Promise<void> {
  const pluginRoot = getPluginRoot();
  process.env.VELLUM_PLUGIN_ROOT = pluginRoot;
  if (ctx.pluginStorageDir) {
    process.env.VELLUM_PLUGIN_STORAGE_DIR = ctx.pluginStorageDir;
  }

  hookLog("init_start", { pluginRoot });

  // 1. Disable Vellum's default memory system.
  if (ctx.pluginStorageDir) {
    disableMemoryInConfig(ctx.pluginStorageDir);
    disableDefaultMemoryPlugins(ctx.pluginStorageDir);
  }

  // 2. Validate the host-supplied config (InitContext.config) and resolve the
  //    backend. No config supplied → a local, assistant-managed server;
  //    a supplied config → a remote server unless it asks to be managed.
  const { config: cfg, fromContext, warnings } = validateConfig(ctx.config);
  for (const w of warnings) {
    ctx.logger.warn({ field: w }, `cognee config: ${w}`);
  }
  // Persist the resolved config so the runtime hooks (which read the state
  // file via loadConfig) operate on the same values init resolved.
  saveConfig(cfg);
  const { baseUrl } = cfg;
  hookLog("init_config", {
    managed: cfg.managed,
    mode: cfg.mode,
    baseUrl,
    fromContext,
    warnings: warnings.length,
  });

  // 3. Bring up (managed) or locate (remote) the backend.
  let reachable: boolean;
  if (cfg.managed) {
    // The plugin owns the lifecycle: provision a venv if needed and spawn it.
    reachable = await ensureLocalServer(cfg, ctx.logger);
  } else {
    reachable = await backendReachable(baseUrl);
  }
  if (!reachable) {
    hookLog("init_backend_unreachable", { baseUrl, managed: cfg.managed });
    ctx.logger.warn(
      { baseUrl, managed: cfg.managed },
      "cognee backend not reachable — memory hooks will be no-ops until it comes up",
    );
    // Don't fail init — the backend may come up later.
  } else {
    markServerReady();
  }

  // 4. Resolve or mint the API key.
  let apiKey = resolveApiKey(baseUrl);
  if (!apiKey && reachable && isLocalUrl(baseUrl)) {
    apiKey = await mintApiKey(baseUrl);
  }
  if (!apiKey) {
    hookLog("init_no_api_key", { baseUrl });
    ctx.logger.warn(
      { baseUrl },
      "no cognee API key resolved — set COGNEE_API_KEY env var or ensure the local server is running",
    );
  }

  // 5. Ensure the dataset exists.
  if (reachable && apiKey) {
    await ensureDataset(baseUrl, apiKey, cfg.dataset);
  }

  // 6. Check if the server has an LLM API key configured.
  // Without it, graph sync (/api/v1/remember) will fail with
  // LLMAPIKeyNotSetError. Session cache (/api/v1/remember/entry) works fine.
  if (reachable && apiKey) {
    const hasLlmKey = await checkLlmKey(baseUrl, apiKey);
    if (hasLlmKey === false) {
      hookLog("init_no_llm_key", { baseUrl });
      ctx.logger.warn(
        { baseUrl },
        "cognee server has no LLM API key configured — session-to-graph sync will fail " +
          "until one is set. Session memory (QA pairs, traces) still works. " +
          "Set an LLM key on the cognee server via POST /api/v1/settings or " +
          "the LLM_API_KEY env var on the server process.",
      );
    }
  }

  // 7. Register the agent connection.
  // The conversation ID isn't available at init time — we'll register
  // per-conversation in the user-prompt-submit hook instead.
  // For now, just touch the activity file.
  touchActivity();

  hookLog("init_complete", { baseUrl, hasKey: Boolean(apiKey), reachable });

  ctx.logger.info(
    { baseUrl, mode: cfg.mode, dataset: cfg.dataset },
    "cognee memory initialized — Vellum default memory disabled",
  );
}
