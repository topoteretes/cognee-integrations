#!/usr/bin/env node
import { readFileSync, writeFileSync } from "node:fs";
import { resolve, join } from "node:path";
import { CogneeHttpClient } from "./client.js";
import { resolveConfig } from "./plugin.js";
import { hash, Outbox } from "./runtime.js";

const cfg = resolveConfig();
const directory = resolve(process.cwd());
const client = new CogneeHttpClient(cfg.baseUrl, cfg.apiKey, cfg.username, cfg.password, cfg.requestTimeoutMs, cfg.ingestionTimeoutMs, cfg.mode);
const command = process.argv[2];
try {
  if (command === "health") {
    console.log(JSON.stringify(await client.health()));
  } else if (command === "status") {
    const queue = new Outbox([cfg.baseUrl, hash(cfg.apiKey || cfg.username || "default"), cfg.datasetName, directory].join("|"), cfg.stateDir);
    console.log(JSON.stringify({backend: cfg.baseUrl, dataset: cfg.datasetName, capture: cfg.autoCapture, recall: cfg.autoRecall, outbox: queue.status()}, null, 2));
  } else if (command === "setup") {
    const path = join(directory, "opencode.json");
    let config: Record<string, unknown> = {};
    try { config = JSON.parse(readFileSync(path, "utf8")); } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    const plugins = Array.isArray(config.plugin) ? config.plugin : [];
    if (!plugins.some((p) => typeof p === "string" && p.startsWith("@cognee/cognee-opencode"))) plugins.push("@cognee/cognee-opencode");
    config.plugin = plugins;
    writeFileSync(path, JSON.stringify(config, null, 2) + "\n", {mode: 0o600});
    console.log("Configured opencode.json. Set COGNEE_BASE_URL and COGNEE_API_KEY in your shell, then run cognee-opencode health.");
  } else if (command === "index") {
    const file = process.argv[3];
    if (!file) throw new Error("Usage: cognee-opencode index <text-file>");
    // Explicit indexing is user-directed and independent of automatic capture filters.
    const text = readFileSync(resolve(file), "utf8");
    await client.remember({data: text, datasetName: cfg.datasetName});
    console.log("Indexed in " + cfg.datasetName);
  } else {
    console.log("Usage: cognee-opencode <setup|health|status|index <text-file>>");
    if (command) process.exitCode = 2;
  }
} catch (error) {
  // Backend errors can echo submitted secrets; status is enough for CLI diagnosis.
  console.error("cognee-opencode: command failed; check configuration, file access and backend health.");
  process.exitCode = 1;
}
