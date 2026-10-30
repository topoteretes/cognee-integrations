// Cognee memory status in Cursor's status bar.
//
// The Cognee Cursor plugin runs as hooks and has no UI of its own in the IDE.
// This extension draws the same line the Cursor CLI status line shows, by running
// the plugin's own renderer (scripts/cognee_statusline_render.py), and switches the
// chat's dataset through the plugin's scripts/switch-dataset.py. It keeps no state
// and talks to no server itself: everything comes from the plugin.

"use strict";

const vscode = require("vscode");
const childProcess = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const STATE_DIR = path.join(os.homedir(), ".cognee-plugin", "cursor");
const LAST_RECALL = path.join(STATE_DIR, "last_recall.json");
const PLUGINS_DIR = path.join(os.homedir(), ".cursor", "plugins");
const RENDERER = "cognee_statusline_render.py";
const SWITCHER = "switch-dataset.py";
const REFRESH_MS = 30_000;
const DEBOUNCE_MS = 500;
const RUN_TIMEOUT_MS = 20_000;
// Files the plugin's hooks rewrite as a chat goes on; a change means a new line.
const WATCHED = new Set([
  "last_recall.json",
  "save_counter.json",
  "counter.json",
  "credits.json",
  "activity.ts",
]);

function stripAnsi(text) {
  return text.replace(/\x1b\[[0-9;]*m/g, "");
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return {};
  }
}

function newestMatch(dirs) {
  let best = "";
  let bestTime = -1;
  for (const dir of dirs) {
    try {
      const time = fs.statSync(path.join(dir, RENDERER)).mtimeMs;
      if (time > bestTime) {
        best = dir;
        bestTime = time;
      }
    } catch {
      // not a plugin copy
    }
  }
  return best;
}

function listDirs(dir) {
  try {
    return fs
      .readdirSync(dir, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => path.join(dir, entry.name));
  } catch {
    return [];
  }
}

// The plugin's scripts folder: the setting, else the newest installed copy
// (marketplace cache: cache/<marketplace>/cognee-memory/<commit>/; local folder:
// local/<name>/).
function scriptsDir() {
  const configured = vscode.workspace.getConfiguration("cognee").get("pluginRoot", "");
  if (configured) {
    return path.join(configured, "scripts");
  }
  const candidates = [];
  for (const marketplace of listDirs(path.join(PLUGINS_DIR, "cache"))) {
    for (const commit of listDirs(path.join(marketplace, "cognee-memory"))) {
      candidates.push(path.join(commit, "scripts"));
    }
  }
  for (const local of listDirs(path.join(PLUGINS_DIR, "local"))) {
    candidates.push(path.join(local, "scripts"));
  }
  return newestMatch(candidates);
}

function python() {
  const configured = vscode.workspace.getConfiguration("cognee").get("python", "");
  return configured || (process.platform === "win32" ? "python" : "python3");
}

// The chat the plugin last recalled for. An extension can't see which Cursor chat
// has focus, so the status follows the most recent prompt.
function currentSessionKey() {
  return String(readJson(LAST_RECALL).session_key || "");
}

function workspaceDir() {
  const folder = vscode.workspace.workspaceFolders?.[0];
  return folder ? folder.uri.fsPath : "";
}

function run(script, args, input) {
  return new Promise((resolve) => {
    const child = childProcess.execFile(
      python(),
      [script, ...args],
      { timeout: RUN_TIMEOUT_MS, maxBuffer: 1 << 20, env: { ...process.env, PYTHONIOENCODING: "utf-8" } },
      (error, stdout, stderr) => resolve({ error, stdout: String(stdout), stderr: String(stderr) }),
    );
    child.stdin.on("error", () => {});
    child.stdin.end(input ?? "");
  });
}

// "● cognee: agent_sessions · cloud · credits: $5.00 · 1 memory hit · 8/8 turns…"
// -> bar text without the credits and the per-session ratio, which stay in the tooltip.
function compact(line) {
  const parts = line.split(" · ");
  const kept = parts.filter(
    (part, index) =>
      index < 2 || /memory hits?\b|no memory|update available|⬆/i.test(part),
  );
  return kept.join(" · ").replace(/^● /, "").replace(/^cognee: /, "cognee ");
}

class Status {
  constructor(context) {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    this.item.command = "cognee.switchDataset";
    this.item.name = "Cognee Memory";
    this.timer = undefined;
    this.debounce = undefined;
    this.watcher = undefined;
    this.busy = false;
    context.subscriptions.push(this.item, { dispose: () => this.dispose() });
  }

  start() {
    this.refresh();
    this.timer = setInterval(() => this.refresh(), REFRESH_MS);
    try {
      this.watcher = fs.watch(STATE_DIR, (_event, name) => {
        if (name && WATCHED.has(String(name))) {
          clearTimeout(this.debounce);
          this.debounce = setTimeout(() => this.refresh(), DEBOUNCE_MS);
        }
      });
      this.watcher.on("error", () => {});
    } catch {
      // no state dir yet; the interval keeps trying
    }
  }

  async refresh() {
    if (this.busy) {
      return;
    }
    this.busy = true;
    try {
      const scripts = scriptsDir();
      if (!scripts) {
        this.item.hide();
        return;
      }
      const context = JSON.stringify({ session_id: currentSessionKey(), cwd: workspaceDir() });
      const { stdout } = await run(path.join(scripts, RENDERER), [], context);
      const line = stripAnsi(stdout).trim();
      if (!line) {
        this.item.hide();
        return;
      }
      const failing = line.startsWith("✕");
      this.item.text = `$(pulse) ${compact(line)}`;
      this.item.tooltip = `${line}\n\nClick to switch this chat's dataset.`;
      this.item.backgroundColor = failing
        ? new vscode.ThemeColor("statusBarItem.errorBackground")
        : undefined;
      this.item.show();
    } finally {
      this.busy = false;
    }
  }

  dispose() {
    clearInterval(this.timer);
    clearTimeout(this.debounce);
    this.watcher?.close();
  }
}

const LOADING_ITEM = { label: "$(loading~spin) Loading datasets…", alwaysShow: true, loading: true };

// The picker opens before anything else runs, with a spinning "Loading datasets…"
// row, and fills in when the server answers: listing asks the server and takes a
// few seconds.
async function switchDataset(status) {
  const picker = vscode.window.createQuickPick();
  picker.title = "Cognee: switch this chat's dataset";
  picker.placeholder = "Loading datasets…";
  picker.items = [LOADING_ITEM];
  picker.show();

  let closed = false;
  const choice = new Promise((resolve) => {
    picker.onDidAccept(() => {
      const item = picker.selectedItems[0];
      if (item && !item.loading) {
        resolve(item);
        picker.hide();
      }
    });
    picker.onDidHide(() => {
      closed = true;
      resolve(undefined);
    });
  });

  const scripts = scriptsDir();
  const sessionKey = currentSessionKey();
  if (!scripts || !sessionKey) {
    picker.dispose();
    vscode.window.showWarningMessage(
      "Cognee: no Cognee chat yet. Send a message in a Cursor chat first, then switch its dataset.",
    );
    return;
  }
  const switcher = path.join(scripts, SWITCHER);
  const listed = await run(switcher, ["--list", "--json", "--session-key", sessionKey]);
  if (closed) {
    picker.dispose();
    return;
  }
  let data;
  try {
    data = JSON.parse(listed.stdout);
  } catch {
    picker.dispose();
    vscode.window.showErrorMessage(
      `Cognee: could not list datasets. ${stripAnsi(listed.stderr || listed.stdout).trim()}`,
    );
    return;
  }
  const items = (data.datasets || [])
    .filter((dataset) => dataset.writable !== false)
    .map((dataset) => ({ label: dataset.name, description: dataset.current ? "current" : "" }));
  picker.items = items;
  picker.activeItems = items.filter((item) => item.description === "current");
  picker.placeholder = `Current: ${data.current || "unknown"}`;

  const picked = await choice;
  picker.dispose();
  if (!picked || picked.label === data.current) {
    return;
  }
  const switched = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Cognee: switching to ${picked.label}…` },
    () => run(switcher, [picked.label, "--json", "--session-key", sessionKey]),
  );
  let result = {};
  try {
    result = JSON.parse(switched.stdout);
  } catch {
    // fall through to the error below
  }
  if (switched.error || result.ok === false || result.error) {
    const detail = result.error || stripAnsi(switched.stderr || switched.stdout).trim();
    vscode.window.showErrorMessage(`Cognee: could not switch to ${picked.label}. ${detail}`);
  } else {
    vscode.window.showInformationMessage(`Cognee: this chat now uses ${picked.label}.`);
  }
  status.refresh();
}

function activate(context) {
  const status = new Status(context);
  context.subscriptions.push(
    vscode.commands.registerCommand("cognee.switchDataset", () => switchDataset(status)),
    vscode.commands.registerCommand("cognee.refreshStatus", () => status.refresh()),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("cognee")) {
        status.refresh();
      }
    }),
  );
  status.start();
}

function deactivate() {}

module.exports = { activate, deactivate, compact, stripAnsi };
