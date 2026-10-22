<div align="center">
  <a href="https://www.cognee.ai">
    <img src="https://raw.githubusercontent.com/topoteretes/cognee-integrations/main/assets/cognee-logo.svg" alt="Cognee" width="260">
  </a>
  <p><strong>Cognee memory for Cursor</strong> — persistent knowledge-graph memory with automatic capture of prompts, tool traces and answers, and relevant recall on every prompt.</p>
  <p>
    <a href="https://docs.cognee.ai">Docs</a> ·
    <a href="https://discord.gg/NQPKmU5CCg">Discord</a> ·
    <a href="https://github.com/topoteretes/cognee">Cognee core</a>
  </p>
</div>

# Cognee Cursor Plugin

Adds persistent Cognee memory to [Cursor](https://cursor.com) — the IDE agent, Cmd+K and the `cursor-agent` CLI — through [Cursor hooks](https://cursor.com/docs/hooks).

The integration:

- captures prompts, tool traces and assistant answers into Cognee session memory
- injects relevant memory into the model's context: at session start, and per prompt on that turn's first tool call (see below)
- syncs session memory into graph memory when the conversation ends
- ships the Cognee skills (`memory`, `codebase`, `setup`, `cognee-forget`, `cognee-switch-datasets`, `local-ui`) so the agent can remember, search and forget on request

> **Status: 0.1.0 — first working cut.** The hook runtime is the Codex plugin's, so storage, recall, local/cloud modes, dataset switching and the skills behave as documented for [Codex](../codex/README.md) and [Claude Code](../claude-code/README.md). What is new and Cursor-specific is the payload adapter, described below. Read [Known limitations](#known-limitations) before relying on it.

## How it plugs into Cursor

Cursor spawns one process per hook, writes a JSON payload to stdin and reads a JSON reply from stdout. `scripts/cursor_hook.py` sits between Cursor and the shared Cognee hook scripts:

| Cursor hook | Cognee hook script | What happens |
| --- | --- | --- |
| `sessionStart` | `session-start.py` | boots/checks the Cognee server, registers the conversation, returns `additional_context` |
| `beforeSubmitPrompt` | `session-context-lookup.py`, `store-user-prompt.py` | one recall request and the prompt parked for the answer; the reply is only `{"continue": true}`, because Cursor documents no context field for this hook. The memory block is held for the turn (`~/.cognee-plugin/cursor/prompt-context/`) |
| `postToolUse`, `postToolUseFailure` | `store-to-session.py` | tool call + result stored as a trace entry (`Shell`→`Bash`, `Task`→`Agent`, `MCP:x`→`mcp__x`); on the turn's first tool call, the held memory block is returned as `additional_context`, which Cursor documents to reach the model |
| `afterAgentResponse` | `store-to-session.py --stop` | prompt + final answer stored as one QA pair — this is the end-of-turn hook the Cursor IDE actually fires, and its `text` is the answer |
| `stop` | `store-to-session.py --stop`, `credits-refresh.py` | fallback only: skipped when `afterAgentResponse` already stored the turn (a per-conversation marker under `responses/`); otherwise the answer is scraped from the tail of Cursor's JSONL transcript. Never observed to fire in IDE 3.16.17 / CLI 2026.09.26 |
| `preCompact` | `pre-compact.py` | memory anchor / deferred sync |
| `sessionEnd` | `sync-session-to-graph.py --session-end` | session memory bridged into the graph |

Field mapping: `conversation_id` → `session_id`, `generation_id` → `turn_id` (stable for one user turn, so the parked prompt meets its answer), `workspace_roots[0]` / `cwd` → `cwd`, `transcript_path` → `transcript_path`. The Cognee session id is `cursor_<conversation_id>`; plugin state lives in `~/.cognee-plugin/cursor/` (`hook.log` is the first place to look), while the server, venv and API key are shared with the other Cognee plugins under `~/.cognee-plugin/`.

Every adapter path fails open: on any error it prints the hook's neutral reply (`{"continue": true}` on `beforeSubmitPrompt`, `{}` elsewhere) and exits 0. A `stop` hook never returns a `followup_message`.

## Install

**Requirements.** Python 3.9+ as `python3` (or `python`) on PATH — the hooks are stdlib-only HTTP clients. In local mode the plugin builds its own uv-managed Python 3.12 venv for the Cognee server under `~/.cognee-plugin/`; cloud mode builds nothing. See [`CONFIGURATION.md`](../CONFIGURATION.md#python-version-requirements).

### Option A — from a marketplace (default)

Cursor distributes plugins as Git repositories listed in a [marketplace](https://cursor.com/docs/plugins). This repository is a multi-plugin repo: the root [`.cursor-plugin/marketplace.json`](../../.cursor-plugin/marketplace.json) lists `cognee-memory` with `integrations/cursor` as its source, the same way [`.claude-plugin/marketplace.json`](../../.claude-plugin/marketplace.json) does for Claude Code.

- **Cursor Marketplace** — once the plugin is listed at [cursor.com/marketplace](https://cursor.com/marketplace), open **Customize** in the sidebar, search for `cognee-memory`, select **Install** and pick user or project scope.
- **Team marketplace** (Teams/Enterprise) — Dashboard → *Plugins & MCPs* → *Add Marketplace* → *Import from Repo* with `https://github.com/topoteretes/cognee-integrations`. Turn on *Auto Refresh* to follow `main`, then install from **Customize**, or set the plugin to *Default On* for the team.

Either way Cursor installs the plugin itself and keeps it updated; no files need to be copied.

### Option B — local plugin folder (development)

Cursor also loads plugins copied into `~/.cursor/plugins/local/`:

```bash
git clone https://github.com/topoteretes/cognee-integrations.git
mkdir -p ~/.cursor/plugins/local
cp -R cognee-integrations/integrations/cursor ~/.cursor/plugins/local/cognee-memory
```

Then **Developer: Reload Window** (or restart Cursor) and check *Customize → Plugins* for `cognee-memory`; the *Hooks* tab lists the registered hooks. Symlinks into `~/.cursor/plugins/local` are ignored by Cursor unless they resolve inside that folder, hence the copy. Team admins can disable local plugin imports (off by default on Enterprise), and a marketplace plugin with the same name takes precedence over the local copy.

### Option C — hooks in `hooks.json` (no plugin)

Registers the same hooks directly in Cursor's `hooks.json`, without the plugin, for machines where plugin installation is not allowed:

```bash
# user level: ~/.cursor/hooks.json (all your projects)
python3 integrations/cursor/scripts/install-cursor-hooks.py

# project level: <repo>/.cursor/hooks.json
python3 integrations/cursor/scripts/install-cursor-hooks.py --project /path/to/repo

python3 integrations/cursor/scripts/install-cursor-hooks.py --print      # preview
python3 integrations/cursor/scripts/install-cursor-hooks.py --uninstall  # remove
```

The installer merges into an existing `hooks.json`, leaves other hooks alone, and is idempotent: it recognises its own entries as a `run-cursor-hook` launcher running one of the plugin's hook scripts. A `hooks.json` it cannot parse safely (`hooks` not an object, an event that is not a list) is left untouched with an error. Cursor reloads `hooks.json` on save; open a new conversation to start capturing.

- **The commands hold this checkout's absolute path.** Moving or deleting the checkout breaks the hooks until you rerun the installer from the new location. For the same reason, don't commit a project-level `hooks.json` for teammates or **cloud agents**: the paths don't exist on their machines.
- **Use this option *or* the plugin, not both.** Cursor runs plugin hooks and `hooks.json` hooks side by side, so with both every prompt, tool call and answer is captured twice. Remove these entries with `--uninstall` before installing the plugin. Skills come only with the plugin (Option A or B).

### Configure the runtime

Configure the mode **once** in `~/.cognee/.env`; the file is shared with the Claude Code and Codex plugins and is created with a commented template on the first session start.

**Cognee Cloud or a remote server:**

```bash
mkdir -p ~/.cognee
cat >> ~/.cognee/.env <<'EOF'
COGNEE_BASE_URL="https://your-instance.cognee.ai"
COGNEE_API_KEY="ck_..."
EOF
chmod 600 ~/.cognee/.env
```

**Local mode** (default when `COGNEE_BASE_URL` is unset) — the plugin boots a local Cognee API at `http://localhost:8011`; only an LLM key is needed:

```bash
mkdir -p ~/.cognee
cat >> ~/.cognee/.env <<'EOF'
LLM_API_KEY="sk-..."
EOF
chmod 600 ~/.cognee/.env
```

`COGNEE_BACKEND=local|cloud` pins the mode for every Cognee plugin; `COGNEE_CURSOR_BACKEND` does the same for this plugin only. The IDE's hooks inherit the Cursor app's environment, not a terminal's, so put these in `~/.cognee/.env` (re-read by every hook) rather than exporting them in a shell, unless you start Cursor from that shell. All other knobs are the Codex plugin's and are documented in the [Codex README](../codex/README.md): `COGNEE_PLUGIN_DATASET` under *Dataset*, `COGNEE_CAPTURE` and the `COGNEE_CAPTURE_*` allow/deny/redaction lists under capture, and the improve cooldowns under *Session sync and watchers*.

## Verify

Open a new agent conversation and run one tool. Then:

```bash
tail -n 20 ~/.cognee-plugin/cursor/hook.log
# the installed plugin's own copy; the agent's session context also names it ("Cognee plugin root: …")
python3 "$(find ~/.cursor/plugins -path '*/scripts/doctor.py' -path '*cognee*' | head -n 1)" --json
```

`hook.log` shows a `store_session_key` event with `"source": "payload.session_id"` for each hook, `trace_stored` after tool calls and `stop_stored` after an answer (each line also carries the dotted `event_name`, e.g. `store.session_key`; see [EVENTS.md](EVENTS.md)). `~/.cognee-plugin/cursor/adapter.log` has one line per hook Cursor actually launched — Cursor event, inner script, conversation/turn, `outcome` `ran` / `skipped` / `failed` and duration — which is the place to look when a stage seems missing (the inner scripts log nothing when the adapter skips them). Cursor's own **Hooks** output channel (Cmd+Shift+P → *Hooks*) shows every hook launch and any error. Once a turn runs a tool, the context added after that tool result begins with the `Cognee memory: … memory hits …` header.

## Status line (Cursor CLI)

In the Cursor CLI (`cursor-agent` / `agent`) the plugin can draw the same status line as the Claude Code plugin above the prompt. It is **opt-in** (`COGNEE_STATUSLINE=true` in `~/.cognee/.env`), because a custom status line replaces the CLI's own footer (model, approval state) rather than adding a row:

```
● cognee: agent_sessions · local · 5 memory hits · 12/40 turns had hits this session
```

The glyph is the connection/LLM-key health (green `●`, red `✕ …` with the reason), then the dataset and mode, cloud credits when applicable, this turn's recall hits with the session's activation ratio, and an amber `⬆ Cognee update available` when one is.

With the opt-in set, `sessionStart` registers it: `scripts/_statusline_config.py` writes `statusLine` into `~/.cursor/cli-config.json` pointing at `scripts/cognee-statusline.sh` of the running plugin copy (the CLI spawns the command directly, without a shell). Without it (or with `COGNEE_STATUSLINE=false`) the plugin's own entry is removed again. A `statusLine` you configured yourself is never replaced or removed; the plugin's own entry is updated in place when the plugin moves and removed by the renderer when the plugin folder is gone. Edits take a lock, yield to a concurrent write by the CLI, keep a symlinked `cli-config.json` a symlink and keep its permissions. Knobs: `COGNEE_STATUSLINE_TIMEOUT_MS` / `COGNEE_STATUSLINE_PADDING` map to the CLI's `timeoutMs` / `padding`, `COGNEE_STATUSLINE_COUNTS=full|false` switches the recall segment to the per-scope strip or hides it. The renderer is pure-local (state files under `~/.cognee-plugin/cursor/`, no network). Register it by hand with `python3 scripts/_statusline_config.py` (`--remove` to undo); restart the CLI session to pick up a new `cli-config.json`.

The Cursor IDE has no status line. There, the same text (without ANSI) is the first line of the context the plugin injects at session start and on each turn's first tool call, so the agent can relay it.

## Known limitations

- **Per-prompt recall needs a tool call.** Cursor documents no way for `beforeSubmitPrompt` to add context, so the recall made there reaches the model with the turn's first `postToolUse`. A turn that answers without calling any tool gets only the session-start context.
- **Windows is untested in Cursor.** The shared test suite runs on Windows CI, but `scripts/run-cursor-hook.cmd` is not used by the plugin's `hooks/hooks.json`, which calls the POSIX launcher; on Windows use `install-cursor-hooks.py`, which picks the `.cmd`. The status line launcher is POSIX-only too.
- **No user-facing notices in the IDE.** Cursor has no channel for a non-blocking hook message. Notices the other plugins show as a `systemMessage` (memory off, update available) are appended to the model's context as `[cognee notice] …` so the agent can relay them; in the CLI the status line shows the same states.
- **`preCompact` cannot inject context** in Cursor, so the memory anchor the Claude Code plugin injects before compaction is not available; the hook still defers the sync.
- **Headless CLI runs (`cursor-agent -p …`) fire only `sessionStart`, `postToolUse`/`postToolUseFailure` and `sessionEnd`** (observed with CLI 2026.09.26). The prompt passed on the command line does not go through `beforeSubmitPrompt`, and `afterAgentResponse`/`stop` do not fire, so headless runs store tool traces (which later recall finds) but not the prompt or the answer, and get no recall injected. Interactive sessions get the full lifecycle.
- **Cursor's `stop` hook has not been seen to fire** (IDE 3.16.17, CLI 2026.09.26; `~/.cognee-plugin/cursor/adapter.log` records every hook launch). That is why the QA pair is stored from `afterAgentResponse`; `credits-refresh.py`, registered on `stop`, therefore rarely runs and the credits segment of the status line may lag.
- **Cloud agents are not supported yet.** They run project hooks only, and `install-cursor-hooks.py` writes absolute paths into your checkout that don't exist on the agent's machine. A cloud agent would also need `COGNEE_BASE_URL` and `COGNEE_API_KEY` as secrets, since it has no `~/.cognee/.env`, and `sessionStart`/`sessionEnd` don't fire there.
- **Double capture with the Claude Code plugin.** Cursor's Claude Code compatibility layer (Settings → Agents → Third-Party Imports, on by default) also loads the *Claude Code* Cognee plugin's hooks, so both plugins capture the same conversation. You can tell when the same prompt shows up in both `~/.cognee-plugin/claude-code/hook.log` and `~/.cognee-plugin/cursor/hook.log`. Pick one: uninstall this plugin, or keep it and turn the third-party import off. That switch covers every Claude Code import (rules, skills, other hooks), not just Cognee.

## Development

Unit tests live in the shared suite:

```bash
cd integrations/tests
uv run pytest tests/unit/test_cursor_payload_adapter.py tests/unit/test_cursor_hooks_contract.py tests/unit/test_cursor_exit_watcher.py -q
COGNEE_TEST_SUITE=cursor uv run pytest tests/unit -q   # the shared suite against this copy
```

`scripts/` is a copy of the Codex plugin's hook runtime with Cursor's constants (`config.py`, `_plugin_common.py`, `hook_runner.py`, `_code_graph.py`, `_env_file.py`) and a few Cursor-specific changes (host detection and per-chat exit watchers in `session-start.py`, the status line). Keep it in step with `integrations/codex/plugins/cognee/scripts/` when that runtime changes. The shared suite runs against it as the `cursor` suite (`integrations/tests/utils/suites.py`), so a Codex fix that isn't carried over shows up as a failing test. `cursor_hook.py`, `run-cursor-hook`, `install-cursor-hooks.py`, `_statusline_config.py` and `hooks/hooks.json` are Cursor-only.
