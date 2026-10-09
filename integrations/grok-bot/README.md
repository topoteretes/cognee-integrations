<div align="center">
  <a href="https://www.cognee.ai">
    <img src="https://raw.githubusercontent.com/topoteretes/cognee-integrations/main/assets/cognee-logo.svg" alt="Cognee" width="260">
  </a>
  <p><strong>Cognee memory for Grok</strong> — persistent knowledge-graph memory for Grok Build (hooks: automatic capture and recall on every turn) and Grok Bot (remote MCP).</p>
  <p>
    <a href="https://docs.cognee.ai">Docs</a> ·
    <a href="https://discord.gg/NQPKmU5CCg">Discord</a> ·
    <a href="https://github.com/topoteretes/cognee">Cognee core</a>
  </p>
</div>

# Cognee Memory Plugin for Grok

Adds persistent memory to xAI's Grok agents through Cognee. One plugin directory
serves two hosts, because they load plugins differently:

| Host | What it is | How this plugin works there |
|---|---|---|
| **Grok Build** (`grok` CLI) | xAI's terminal coding agent. Loads plugins in the `.grok-plugin/plugin.json` layout — the same one Claude Code uses — and runs plugin **hooks** locally. | Full parity with the Claude Code plugin: hooks capture prompts, tool traces and answers into session memory, inject relevant context on every prompt, and sync the session into the graph at exit. Talks to a Cognee server over HTTP (local, self-hosted, or Cognee Cloud). |
| **Grok Bot** (cloud) | xAI's always-on agent on a persistent cloud VM. Its Settings → Plugins list is a Cursor Marketplace catalog, and it only attaches **remote MCP servers** — never localhost or stdio. | The same directory carries a `.cursor-plugin/plugin.json` + `mcp.json` that register a public `cognee-mcp` HTTP endpoint as the `cognee` MCP server (`remember` / `recall` / `forget` tools), plus skills that tell the Bot when to use them. |

Grok Bot's runtime does not document plugin hooks, so the hook path is treated as
Grok Build-only until it is proven on a real Bot session (see
[Status and caveats](#status-and-caveats)).

The integration:
- captures prompts, tool traces, and assistant responses into session memory
- injects relevant context on prompt submit
- syncs session memory into graph memory on session end/final exit
- deletes memory on request via the `cognee-forget` skill ("forget what we talked about X")
- indexes git repositories into a deterministic code graph and answers structural code questions from it

## Install — Grok Build

Add the marketplace and install from your shell, *before* launching Grok, so the
first `grok` launch is a clean session that runs the plugin bootstrap:

```bash
grok plugin marketplace add topoteretes/cognee-integrations
grok plugin install cognee-memory@cognee --trust
```

Grok also reads Claude Code marketplaces directly, so if the `cognee` Claude Code
marketplace is already registered on the machine the Claude Code plugin shows up
too. Install **this** one for Grok — it keeps its own state directory and session
prefix so the two never step on each other, while sharing the configuration file,
the local server and the `agent_sessions` dataset.

For a checkout you are editing, install straight from the local path (no
marketplace needed; `grok plugin validate <path>` checks the manifest first):

```bash
grok plugin validate /path/to/cognee-integrations/integrations/grok-bot
grok plugin install  /path/to/cognee-integrations/integrations/grok-bot --trust
```

A local-path install **copies** the directory into
`~/.grok/installed-plugins/` (even though `grok plugin update` reports "local
symlink, already live", the copy is not refreshed), so after editing files
reinstall it:

```bash
grok plugin uninstall cognee-memory
grok plugin install /path/to/cognee-integrations/integrations/grok-bot --trust
```

Then configure your runtime mode — **once** — in `~/.cognee/.env`. The file is created with a commented template on the first session start; values in it act exactly like shell exports (a real `export` in your shell still overrides the file, per terminal). It is shared with the Claude Code and Codex plugins, so all three read the same configuration. Lines may optionally start with `export `. Pick one of the two modes below — or configure **both** and flip a terminal with a single export (see [Which mode wins, and how to switch](#which-mode-wins-and-how-to-switch)).

**Cognee Cloud or a remote server** — set both (one paste, no editor needed):

```bash
mkdir -p ~/.cognee
cat >> ~/.cognee/.env <<'EOF'
COGNEE_BASE_URL="https://your-instance.cognee.ai"
COGNEE_API_KEY="ck_..."
EOF
chmod 600 ~/.cognee/.env
```

> Cloud mode is a pure thin client: it talks to your remote server over HTTP only and does **not** install a local Cognee runtime. The bundled virtualenv (`~/.cognee-plugin/venv`) is built only in local mode, where an in-process server actually runs.

**Local mode** (default when `COGNEE_BASE_URL` is not set) — the plugin bootstraps a local Cognee API at `http://localhost:8011`. Only `LLM_API_KEY` is required; `COGNEE_API_KEY` is auto-minted if absent:

```bash
mkdir -p ~/.cognee
cat >> ~/.cognee/.env <<'EOF'
LLM_API_KEY="sk-..."
EOF
chmod 600 ~/.cognee/.env
```

Re-running any of these blocks is safe: when a key appears more than once, the **last value wins**. Changes apply on the next session launch. On startup you should see a **"Cognee Memory Connected"** message; it names the plugin's `scripts/` directory so the model can run the skill commands even if the host does not expand `${GROK_PLUGIN_ROOT}` inside skill text.

### Which mode wins, and how to switch

You can configure **both modes at once** — keep `COGNEE_BASE_URL` + `COGNEE_API_KEY` *and* `LLM_API_KEY` in the file together. The mode is then decided per terminal, by three rules in order:

1. **A `COGNEE_BACKEND` export wins.** `export COGNEE_BACKEND=local` (or `=cloud`) pins that terminal to that mode.
2. **Otherwise, cloud wins when configured.** If `COGNEE_BASE_URL` is set (in the file or the shell), the plugin connects to it.
3. **Otherwise, local.** With no URL anywhere, the plugin boots the local server.

```bash
grok                            # → cloud (the configured URL routes)
COGNEE_BACKEND=local grok       # → local, this launch only
export COGNEE_BACKEND=local     # → local for every launch from this shell
```

- The shared `COGNEE_BACKEND` flips the Grok, Claude Code **and** Codex plugins in that terminal. To flip only this one, use `COGNEE_GROK_BACKEND` — the plugin-specific name beats the shared one, and `COGNEE_CLAUDE_BACKEND` / `COGNEE_CODEX_BACKEND` never touch this plugin.
- Accepted values: `local` (aliases: `native`, `sdk`) and `cloud` (aliases: `http`, `api`, `server`).
- `COGNEE_BACKEND=cloud` with no `COGNEE_BASE_URL` configured still counts as cloud: the plugin does **not** silently fall back to local; the recall header shows `✕ (missing_cognee_base_url)`.
- Not sure what a terminal resolved? `python3 "$GROK_PLUGIN_ROOT/scripts/doctor.py"` (or `scripts/cognee-doctor.sh`) prints the decision with its cause.

Every prompt's recalled context opens with a one-line memory header:

```
Cognee memory: 5 memory hits (3 from past sessions) · 12/40 turns had hits this session · saved last turn 1 prompt / 3 trace / 1 answer
```

The counts are also written to `~/.cognee-plugin/grok-bot/last_recall.json`.

### Skills

Grok exposes a plugin's user-invocable skills as slash commands. Memory is captured
and recalled automatically; invoke these when you want to act explicitly:

| Skill | What it does |
|---|---|
| `cognee-remember` | store something permanently, tagged `user_context` / `project_docs` / `agent_actions` |
| `cognee-search` | query session cache + permanent graph (server-first, dataset-scoped) |
| `cognee-forget` | find and delete the documents behind "forget what we said about X" |
| `cognee-sync` | bridge the current session into the permanent graph now |
| `cognee-switch-datasets` | move this session to another dataset |
| `cognee-code` | index a repository into the code graph and query it (callers, impact, paths) |

Skill commands are written as `${GROK_PLUGIN_ROOT}/scripts/<script>`. Grok exports
`GROK_PLUGIN_ROOT` to plugin hooks; the SessionStart message also states the
absolute `scripts/` path, which is what to use if the variable is not expanded in
the model's shell.

## Install — Grok Bot

Grok Bot runs on a cloud VM and only connects to MCP servers that are reachable
over the public internet; `localhost` and private addresses are rejected. Cognee
Cloud does not yet expose a hosted MCP endpoint, so you run one:

**1. Run `cognee-mcp` over HTTP, pointed at your Cognee server**

```bash
pip install cognee-mcp        # or: uvx cognee-mcp ...
cognee-mcp --transport http --host 0.0.0.0 --port 8000 --path /mcp \
  --api-url https://your-instance.cognee.ai --api-token ck_...
```

`--api-url` / `--api-token` (or `COGNEE_BASE_URL` / `COGNEE_API_KEY`) make it a
thin proxy to a running Cognee server — the same one the Grok Build hooks write
to, so both hosts share memory. Without them it runs its own local Cognee and needs
`LLM_API_KEY`.

**2. Expose it over HTTPS** — a tunnel (`cloudflared tunnel --url http://localhost:8000`, ngrok, Tailscale Funnel) or a real deployment. xAI documents the tunnel approach for custom connectors.

**3. Add the plugin to Grok Bot.** Either

- add it as a **custom MCP connector** in Settings → Plugins with the URL
  `https://<your-host>/mcp` (and an `X-Api-Key` header if your endpoint checks one), or
- publish this directory to your **Cursor team marketplace** (Cursor Dashboard →
  Plugins → Team Marketplaces → Import from Repo, pointing at this repository's
  `integrations/grok-bot`). The `.cursor-plugin/plugin.json` manifest declares two
  variables the dashboard prompts for: `COGNEE_MCP_URL` (required) and
  `COGNEE_API_KEY` (optional), substituted into `mcp.json`.

Once connected the Bot sees the `cognee` server's `remember`, `recall` and `forget`
tools, and the plugin skills tell it when to reach for each.

> The bundled `cognee-mcp` takes one API token at startup and does not authenticate
> per request. A shared endpoint for several users therefore needs an
> authenticating proxy in front of it (or a per-user endpoint each); the
> `X-Api-Key` header in `mcp.json` is there for endpoints that do check it.

## Auth

The integration uses a **single auth principal** — one API key, one user. Key resolution order:
1. `COGNEE_API_KEY` env var
2. `~/.cognee-plugin/api_key.json` (cached from a previous mint)
3. Auto-mint from the default local user (local mode only), then cache to `api_key.json`

## Sessions

Each `grok` launch maintains a small map file:

```
~/.cognee-plugin/grok-bot/sessions/<host_session_id>.json
  → { "conn_uuid": "...", "session_id": "...", "host_key": "..." }
```

`session_id` is the Cognee session this launch writes to and recalls from; the host
session id comes from the hook payload (`sessionId`) or the `GROK_SESSION_ID` hook
environment variable. Set `COGNEE_SESSION_ID` to resume a named session; two
terminals can deliberately share one by exporting the same value.

## Dataset

All writes and recall are scoped to a single dataset. By default the Grok, Claude Code and Codex plugins all use `agent_sessions`, so memory is shared across the three automatically. Seed a different one at launch with `COGNEE_PLUGIN_DATASET="my-project-memory"`, or switch mid-session with the `cognee-switch-datasets` skill (the choice lives in the launch record and survives a resume).

## Hooks

Grok Build runs the same lifecycle events as Claude Code. Every entry declares its
own `timeout` because Grok's default is 5 seconds.

| Hook | Behavior |
|---|---|
| `SessionStart` | mode select, identity setup, dataset readiness, watcher bootstrap |
| `UserPromptSubmit` | context lookup + async prompt staging |
| `PostToolUse` | async trace write (matcher `.*` — Grok's tool names are not Claude's) |
| `Stop` / `StopFailure` | assistant answer write, credits refresh |
| `PreCompact` | memory anchor build before compaction |
| `SessionEnd` | trigger detached final sync worker |

Grok's native hook payload is camelCase (`hookEventName`, `sessionId`, `cwd`,
`workspaceRoot`, `toolName`, `toolInput`); the hooks normalize those onto the
snake_case names the shared scripts read, so Claude-compatible and Grok-native
payloads both work. Hook commands reference the plugin as
`${GROK_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}`: Grok exports the first to plugin
hooks, and the second is honored when Grok loads the plugin through its Claude
Code compatibility path.

## Session sync, watchers, code graph

Identical to the Claude Code plugin — an idle watcher bridges the session into the
graph while you are quiet, an exit watcher finishes the job if Grok dies without
firing `SessionEnd`, and opening Grok inside a git repository indexes it into a
code graph in the background (local server only, unless
`COGNEE_CODE_AUTOINDEX=always`). See the
[Claude Code README](../claude-code/README.md#session-sync-and-watchers) for the
tuning variables (`COGNEE_IDLE_*`, `COGNEE_IMPROVE_*`, `COGNEE_CODE_AUTOINDEX`);
they are read from the same `~/.cognee/.env`.

## Status line

Grok Build keeps its configuration in `~/.grok/config.toml` and documents no
`statusLine` setting, so unlike the Claude Code plugin nothing is written into host
settings. The in-context `Cognee memory: …` header carries the connection glyph,
dataset and mode on every prompt. To render the full bar on demand:

```bash
echo '{}' | "$GROK_PLUGIN_ROOT/scripts/cognee-statusline.sh"
```

## Logs and state

```bash
~/.cognee-plugin/grok-bot/
tail -f ~/.cognee-plugin/grok-bot/hook.log
tail -f ~/.cognee-plugin/grok-bot/subprocess.log
tail -f ~/.cognee-plugin/grok-bot/recall-audit.log
tail -f ~/.cognee-plugin/grok-bot/exit-watcher.log
tail -f ~/.cognee-plugin/grok-bot/watcher.log
```

Logs rotate at `COGNEE_PLUGIN_LOG_MAX_BYTES` (default 20 MiB). The shared runtime
(venv, local server, data) lives at `~/.cognee-plugin/` and `~/.cognee/`, exactly
where the Claude Code and Codex plugins keep theirs, so Cognee is installed once.

For an offline usage rollup: `python3 "$GROK_PLUGIN_ROOT/scripts/cognee-plugin" metrics`.

## Updating

```bash
grok plugin marketplace update cognee
grok plugin update cognee-memory
```

A background check in the idle watcher (at most once per `COGNEE_UPDATE_CHECK_INTERVAL`, default hourly) reads the published `.grok-plugin/marketplace.json` on `main` and surfaces a one-time *"Cognee update available"* note at SessionStart. Turn it off with `COGNEE_UPDATE_CHECK=false`.

## Remove

```bash
grok plugin uninstall cognee-memory
grok plugin marketplace remove cognee
```

## Configuration reference

Config precedence: env vars (shell exports) → `~/.cognee/.env` → defaults. The `COGNEE_BACKEND` / `COGNEE_GROK_BACKEND` switch sits above all three.

| Key | Env var(s) | Default | Notes |
|---|---|---|---|
| `dataset` | `COGNEE_PLUGIN_DATASET` | `agent_sessions` | Dataset for writes and recall at launch |
| `session_id` | `COGNEE_SESSION_ID` | auto-generated per launch | Override to resume a named session |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `grok` | Prefix for auto-generated session IDs |
| `base_url` | `COGNEE_BASE_URL` | unset | Set to enable cloud / remote mode |
| `api_key` | `COGNEE_API_KEY` | unset | API key; auto-minted if absent in local mode |
| mode switch | `COGNEE_BACKEND` | unset | `local` or `cloud` — pins the terminal's mode for every Cognee plugin |
| plugin-only mode switch | `COGNEE_GROK_BACKEND` | unset | Same, for this plugin only; beats `COGNEE_BACKEND` |
| local URL override | `COGNEE_LOCAL_API_URL` | `http://localhost:8011` | Local API base URL |
| local LLM | `LLM_API_KEY`, `LLM_MODEL` | unset | Required for local mode |
| Grok Bot MCP endpoint | `COGNEE_MCP_URL` | unset | Public `cognee-mcp` HTTP URL (Cursor plugin variable, `mcp.json`) |

Internal plumbing variables (`COGNEE_USER_ID`, `COGNEE_SESSION_KEY`, `COGNEE_AGENT_SESSION_NAME`, `COGNEE_PLUGIN_IN_VENV`, `COGNEE_SYNC_*`) are written by one hook and read by the workers it spawns — do not set them yourself.

## Testing

**Hermetic suite (no server, no LLM).** The plugin is covered by the shared
harness in `integrations/tests`, parametrized over `claude-code`, `codex` and
`grok-bot`:

```bash
cd integrations/tests
uv sync --dev
uv run pytest tests/ -v -k grok-bot
```

**Real Grok Build session.** From the repo root, with `LLM_API_KEY` (local mode)
or `COGNEE_BASE_URL` + `COGNEE_API_KEY` (cloud) in `~/.cognee/.env`:

```bash
grok plugin install "$PWD/integrations/grok-bot" --trust   # once; or: scripts/install-local-plugins.sh
grok plugin details cognee-memory                          # should list 1 skill dir, 1 agent dir, hooks
grok
```

Expect the *Cognee Memory Connected* message at start. Say something memorable, then
ask about it in the next turn and check the `Cognee memory: …` header reports hits.
`tail -f ~/.cognee-plugin/grok-bot/hook.log` shows every hook firing; the
`mode_decision` lines show which server and key each hook resolved.

**Hook contract by hand.** Any hook can be exercised with a Grok-style payload:

```bash
cd integrations/grok-bot
echo '{"hookEventName":"UserPromptSubmit","sessionId":"manual-test","cwd":"'"$PWD"'","prompt":"what do we know about the payment flow?"}' \
  | GROK_PLUGIN_ROOT="$PWD" python3 scripts/session-context-lookup.py
```

## Status and caveats

- **Grok Build**: the plugin format and hook events are documented by xAI and match
  what the scripts expect; the hook *output* contract (injecting
  `additionalContext` on `UserPromptSubmit`) is documented for Claude Code hosts and
  honored by Grok's Claude Code compatibility layer, but xAI's own hook page says
  stdout is ignored for passive events. If recalled context is not reaching the
  model in a native session, the `recall-audit.log` still proves the lookup ran —
  report it with that log.
- **Grok Bot**: hooks and shipped scripts are not documented for the Bot's cloud
  runtime and should be assumed inert there; memory goes through MCP only. The
  script-based skills (`cognee-sync`, `cognee-switch-datasets`, `cognee-code`) do
  not apply on the Bot.
- **Marketplace listing**: to appear in xAI's official catalog, open a PR against
  `xai-org/plugin-marketplace` adding a `url` source pinned to a 40-character commit
  SHA; for Grok Bot's catalog, submit at `cursor.com/marketplace/publish`.

## Troubleshooting

**Nothing happens at SessionStart** — confirm the plugin loaded (`/plugins` in the
TUI, or `grok plugin list` and `grok plugin details cognee-memory`, which should
list the skills, the agent and the hooks). Then `tail ~/.cognee-plugin/grok-bot/hook.log`.

**Hooks time out** — Grok's default is 5 s; the shipped `hooks.json` sets longer
ones. If you copied hooks elsewhere, carry the `timeout` values.

**Recall returns empty but data was ingested** — recall is scoped to the active
dataset (`agent_sessions` unless you switched). Verify directly:
`curl -X POST "$COGNEE_BASE_URL/api/v1/recall" -H "X-Api-Key: $COGNEE_API_KEY" -d '{"query":"..."}'`.

**Grok Bot rejects the MCP URL** — it must be a public hostname (no `localhost`,
`127.0.0.1`, `10.x`, `172.16.x`, `192.168.x`). Put a tunnel in front of
`cognee-mcp`.

**Unauthorized / key errors** — check `~/.cognee-plugin/api_key.json`; delete it to
force a re-mint in local mode.
