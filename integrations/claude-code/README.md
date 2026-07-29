# Cognee Memory Plugin for Claude Code

Adds persistent memory to Claude Code through Cognee.

The integration:
- captures prompts, tool traces, and assistant responses into session memory
- injects relevant context on prompt submit
- syncs session memory into graph memory on session end/final exit

## Install

Install from the Claude Code marketplace. The recommended way is from your shell, *before* launching Claude Code, so the first `claude` launch is a clean session that runs the plugin bootstrap automatically — no in-app restart needed:

```bash
claude plugin marketplace add topoteretes/cognee-integrations
claude plugin install cognee-memory@cognee
```

These CLI subcommands use the exact same plugin manager as the in-chat `/plugin` commands — same marketplace clone, same install cache, same global state — they just run before you enter the terminal. Default install scope is `user` (global); pass `--scope project` or `--scope local` to confine it.

Then set environment variables for your runtime mode (in the shell you'll launch `claude` from).

**Cognee Cloud or a remote server** — set both:

```bash
export COGNEE_BASE_URL="https://your-instance.cognee.ai"
export COGNEE_API_KEY="ck_..."
```

> Cloud mode is a pure thin client: it talks to your remote server over HTTP only and does **not** install a local Cognee runtime. The bundled virtualenv (`~/.cognee-plugin/venv`) is built only in local mode, where an in-process server actually runs.

**Local mode** (default when `COGNEE_BASE_URL` is not set) — the plugin bootstraps a local Cognee API at `http://localhost:8011`. Only `LLM_API_KEY` is required; `COGNEE_API_KEY` is auto-minted if absent:

```bash
export LLM_API_KEY="sk-..."
```

You can also set config in `~/.cognee-plugin/claude-code/config.json`:

```json
{
  "base_url": "https://your-instance.cognee.ai",
  "dataset": "agent_sessions"
}
```

Then launch `claude`. All setup happens in the `SessionStart` hook, which fires once per fresh launch — so with the shell install above, the first launch connects memory with no extra steps.

If you instead installed **from inside the chat** with the `/plugin` slash commands, you must **restart Claude Code** (start a new session) before memory connects: `/reload-plugins` makes the skills and agents available in the current session but does not run `SessionStart`. On a first-run marketplace install the marketplace is also fetched asynchronously, so `SessionStart` may not fire that session even with a reload. Either way, make sure your env vars are set in the shell you launch from.

On startup you should see a "Cognee Memory Connected" system message.

## Auth

The integration uses a **single auth principal** — one API key, one user.

Key resolution order:
1. `COGNEE_API_KEY` env var
2. `~/.cognee-plugin/api_key.json` (cached from a previous mint)
3. Auto-mint from the default local user (local mode only), then cache to `api_key.json`

## Mode selection rules

At startup (`SessionStart`):
- `COGNEE_BASE_URL` set → `managed_endpoint`, either local, or on Cognee Cloud (API key needed in cloud case)
- otherwise → `integration_local` (local API bootstrap)

At hook runtime:
- hooks resolve mode through runtime endpoint auth (env + `api_key.json`), not only config intent
- `http` mode skips local SDK initialization

The hooks emit `mode_decision` logs with `mode`, `service_url`, `url_source`, `key_source`, `api_key_present`.

## Sessions

Each terminal launch maintains a small map file:

```
~/.cognee-plugin/claude-code/sessions/<host_session_id>.json
  → { "conn_uuid": "...", "session_id": "...", "host_key": "..." }
```

- **`session_id`** — which Cognee session this terminal writes to and recalls from. Fixed at launch.
- **`conn_uuid`** — per-launch liveness handle used for agent registration and server shutdown counting.

By default a new `session_id` is generated each launch. Set `COGNEE_SESSION_ID` to resume a specific session:

```bash
export COGNEE_SESSION_ID="my-project"
```

Two terminals can deliberately share a session by setting the same `COGNEE_SESSION_ID`.

## Dataset

All writes and recall are scoped to a single dataset. By default both the Claude Code and Codex plugins use `agent_sessions`, so memory is shared across both integrations automatically.

Set a custom dataset at launch:

```bash
export COGNEE_PLUGIN_DATASET="my-project-memory"
```

`~/.cognee-plugin/claude-code/config.json` may still show a `dataset` value for
visibility, but runtime dataset selection does not read it.

The dataset is fixed for the lifetime of a launch. Recall searches only the active dataset. If you want to
change the active dataset, you have to exit Claude, change the dataset via env, and then start Claude again.
Data added outside of Claude to the dataset (via SDK or the server for example) is visible in Claude via the Cognee plugin.

## Hooks

| Hook | Behavior |
|---|---|
| `SessionStart` | mode select, identity setup, dataset readiness, watcher bootstrap |
| `UserPromptSubmit` | dataset-scoped context lookup + async prompt staging |
| `PostToolUse` | async trace write |
| `Stop` | assistant answer write + optional transcript clear hook |
| `PreCompact` | memory anchor build before compaction |
| `SessionEnd` | trigger detached final sync worker |

Claude-specific contracts are preserved:
- `hookSpecificOutput` payload format
- async hook behavior for write hooks

## Memory preference

With the plugin active, Cognee is the **preferred** memory system: relevant memory is
auto-recalled into context on every `UserPromptSubmit` and writes are captured
automatically, so Claude consults Cognee first when answering. To reinforce this, the
`SessionStart` hook injects an `additionalContext` instruction telling Claude to treat
Cognee as authoritative and prefer the Cognee tools/skills over Claude Code's built-in
file memory (`MEMORY.md`).

Note: a plugin **cannot reliably disable** Claude Code's native auto memory
(`MEMORY.md` is injected as context, not a tool call that hooks can intercept). This
feature steers the model toward Cognee rather than hard-disabling native memory. To
turn the steer off, set `COGNEE_PREFER_MEMORY=false`. To additionally suppress native
auto memory yourself, disable it in Claude Code (e.g. `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`
in the launching shell, if your Claude Code version supports it).

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_PREFER_MEMORY` | `true` | Inject SessionStart steer asserting Cognee as the preferred memory |

## Session sync and watchers

Session→graph sync runs through Cognee's session-aware `improve` endpoint: the server bridges the session from its own session cache (feedback weights, Q&A persist, compact trace-feedback persist, distillation, enrichment) instead of the plugin re-posting the full accumulated session text — which used to trigger a complete re-cognify of the whole transcript on every sync. Servers without session-aware improve automatically fall back to the legacy document bridge.

An idle watcher runs in the background for the lifetime of each launch. It polls activity every `COGNEE_IDLE_POLL` seconds and fires an improve when the session has been quiet for `COGNEE_IDLE_THRESHOLD` seconds, then waits at least `COGNEE_IMPROVE_COOLDOWN` seconds before the next run. An automatic improve also fires every `COGNEE_AUTO_IMPROVE_EVERY` stored tool calls/stops.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_IDLE_POLL` | `10` | Poll interval in seconds |
| `COGNEE_IDLE_THRESHOLD` | `60` | Seconds of inactivity before idle improve fires |
| `COGNEE_IMPROVE_COOLDOWN` | `600` | Minimum seconds between idle improve runs |
| `COGNEE_AUTO_IMPROVE_EVERY` | `150` | Stored tool calls/stops between automatic improves (0 disables) |
| `COGNEE_IMPROVE_SUBMIT_TIMEOUT` | `180` | Read timeout for the improve POST (distillation runs inside the request) |
| `COGNEE_IMPROVE_POLL_DEADLINE` | `600` | Best-effort wait for cognify/memify completion after submit |
| `COGNEE_IMPROVE_BUSY_DEADLINE` | `600` | How long to wait for a concurrent improve's session lock before giving up |
| `COGNEE_IMPROVE_BUSY_RETRY_INTERVAL` | `15` | Seconds between re-submits while the session lock is held |

Final sync on session end is triggered by the `SessionEnd` detached worker, with an exit watcher as fallback if the process exits without firing `SessionEnd`.

## Skills

- `/cognee-memory:cognee-remember`
- `/cognee-memory:cognee-search`
- `/cognee-memory:cognee-sync`

## Remember (write) behavior

`cognee-remember` and the auto-capture hooks POST to the server's `/api/v1/remember`
and ask it to build the graph **in the background** (`run_in_background=true`), so the
write returns as soon as it's enqueued instead of blocking the turn on a synchronous
cognify. A synchronous build can take tens of seconds, exceed the client timeout, and be
misread as "server unreachable" — which then triggers a `cognee-cli` fallback that can
double-write. The graph populates shortly after the call, so a recall in the same breath
may not see the new entry yet.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_REMEMBER_BACKGROUND` | `true` | Build the graph in the background; set `false` for a synchronous, immediately-queryable write |

A write that *times out* is reported as "submitted; timed out waiting for confirmation"
and does **not** fall back to `cognee-cli` (the write likely landed — a fallback would
risk a duplicate). Only a genuine connection failure falls back.

## Status line

The status line displays `cognee: <dataset> · <mode>`, for example:

```
cognee: agent_sessions · local
cognee: my-project · cloud
```

`<dataset>` is the active Cognee dataset. `<mode>` is `local` when no `COGNEE_BASE_URL` is set or when it points to localhost, and `cloud` when it points to a remote host. The mode is rendered **bold and coloured** — cyan for `local`, magenta for `cloud` — because it is the one field worth a double-take: it tells you which memory you are about to write to. (Red/green/amber are left to the health glyph and the warnings; bold and colour are set together so a terminal that ignores one still shows the other.)

A connection glyph precedes the line:

```
● cognee: agent_sessions · cloud          # connected (server up and authenticated)
✕ (incorrect_cognee_api_key) cognee: … · cloud   # server reachable, but COGNEE_API_KEY was rejected
✕ (unreachable) cognee: … · cloud         # server down / not reachable
✕ (server_error) cognee: … · cloud        # server returned a 5xx
```

`●` shows once the server is confirmed up **and** authenticated. On a failure the glyph flips to `✕ (<reason>)` — `incorrect_cognee_api_key` (a missing, wrong, or expired `COGNEE_API_KEY`), `unreachable` (server down, including a server that dies mid-session), or `server_error` (5xx). The state is recorded by the hooks that already talk to the server (SessionStart, and the per-prompt recall), so the line stays green until a failure is actually observed, and clears back to `●` on the next success. The glyph is read from local state only — no network on refresh. It is **colour-coded**: a bold green `●` when the connection is confirmed good, and a bold red `✕ (<reason>)` — reason included, so the whole verdict reads as one unit — when it is confirmed bad. The LLM-key failure is red as well — the two are told apart by the reason itself (`incorrect_cognee_api_key` for the key this plugin uses to reach the server, `incorrect_llm_api_key` for the key the local server uses to reach the LLM) rather than by colour.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_READY_PROBE_TIMEOUT` | `1.0` | Seconds the per-prompt readiness probe waits before giving up and skipping recall for that turn. It sits on the keystroke→answer path, so the default is deliberately tight; raise it on a slow or loaded server that is otherwise healthy. |

**Local-mode LLM key.** In local mode the plugin also surfaces problems with `LLM_API_KEY` (the key the local server uses to call the LLM) **in that same leading glyph slot**, with its own reasons:

```
✕ (incorrect_llm_api_key) cognee: agent_sessions · local   # missing, or rejected by the provider
```

The slot holds one sign, by precedence: a server-connection failure wins (if the server can't be reached or authenticated, its LLM key isn't the actionable problem), otherwise an LLM-key failure is shown **in place of** the green `●` — the `llm_*` reason already tells you the server side itself is fine, so you never see a contradictory `●` and `✕` side by side.

Both cases show the same reason — the fix is the same either way, and `llm-state.json` still records which of the two it was. Both verdicts come from a single authority: the background idle watcher (never the prompt path). It resolves the key exactly as the server does — Cognee's own config, so a key in `LLM_API_KEY`, a `.env`, or Cognee's config file all count — and validates it with one tiny `max_tokens=1` call through the same LLM stack Cognee uses. That makes it **provider-agnostic**: a rejection is caught for **any** provider (OpenAI, Anthropic, Gemini, Azure, Bedrock, …), not just OpenAI. Only a `401`/`403` counts as a key failure — providers authenticate before validating anything else, so any other response (including the `400` that reasoning models return when one token is too few to finish a message) proves the key works. A transport failure with no HTTP status is inconclusive and leaves the previous verdict alone. It runs once per idle-watcher launch — at session start, and again on any prompt that finds no live watcher (the watcher exits after each idle-bridge cycle) — never more often than once per `COGNEE_LLM_CHECK_INTERVAL` seconds; there is no periodic timer. The verdict clears once the key checks out, and expires after 30 minutes, so one left behind by a session that has ended never haunts the bar. See **Per-terminal status** below for how two terminals that disagree about the key each show their own truth. Local mode only (in cloud the LLM key lives on the remote server). Disable with `COGNEE_LLM_KEY_CHECK=false`.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_LLM_KEY_CHECK` | `true` | Background, provider-agnostic `LLM_API_KEY` validation (local mode) surfacing `✕ (incorrect_llm_api_key)` |
| `COGNEE_LLM_CHECK_INTERVAL` | `300` | Minimum seconds between LLM-key checks |

**Recall counts.** The line ends with what memory actually did, faint so it stays secondary:

```
● cognee: agent_sessions · local · recall 4s/5t/0g/1a · saved 2p/41t/2a
```

`recall` is what this turn's lookup found — `s`ession turns, `t`races, `g`raph context, `a`gent guidance — and `saved` is what the *previous* turn persisted: `p`rompts, `t`races, `a`nswers. These are the same numbers the Codex plugin puts in the `Cognee memory: recall …` header it injects into model context; on Claude Code they live in the bar instead. `UserPromptSubmit` already writes them to `~/.cognee-plugin/claude-code/last_recall.json`, so the renderer stays network-free, and the counts are stamped with the session that produced them so a second terminal's numbers never show up here. Hide them with `COGNEE_STATUSLINE_COUNTS=false`.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_STATUSLINE_COUNTS` | `true` | Show the trailing `recall …/saved …` counts |

**Per-terminal status.** Every signal in the line answers *for this terminal*, not for the machine. That matters because terminals legitimately disagree: one shell exported `LLM_API_KEY` and another didn't, or two hold different `COGNEE_API_KEY`s against the same server. Both sessions show the truth at the same time:

```
terminal A (key exported)  →  ● cognee: agent_sessions · local
terminal B (no key)        →  ✕ (incorrect_llm_api_key) cognee: agent_sessions · local
```

Each writer keeps two records: the machine-wide marker (`server-ready.json`, `llm-state.json`) which stays **coordination** state — it gates recall and is shared with the Codex plugin, since both talk to one server — and a per-session copy under `conn-state/<session_key>.json`, `llm-state/<session_key>.json`, and `recall/<session_key>.json`, which is the **display** state the bar reads. Without the split, a single file meant the last writer decided what every other bar showed: a keyless launch's `not_set` reddening a healthy session, or a healthy one's `ok` hiding a genuinely missing key.

Resolution, in order: this session's own record wins; **except** that a fresher **server-wide** failure in the shared marker takes precedence — `unreachable` or `server_error`, because the server really is shared and a just-observed outage applies to everyone. `incorrect_cognee_api_key` is deliberately **not** propagated: it describes the credential the other session used, not the server, so a keyless cloud terminal starting up can't red a healthy local one. Nor does a fresher shared `ready` clear your own failure — their working key says nothing about yours. With no record of your own, the shared marker is used only when it is unattributed (an older writer, or a write made before the session key was known); a record belonging to another session is ignored and no glyph is drawn, exactly as during warm-up.

**Internal variables — do not set these.** A few `COGNEE_*` names in the environment
are the plugin's own inter-process plumbing, written by one hook and read back by the
detached workers it spawns: `COGNEE_USER_ID` (the resolved Cognee user for this
launch), `COGNEE_SESSION_KEY` (the host session key every hook of a launch resolves
through), `COGNEE_AGENT_SESSION_NAME`, `COGNEE_PLUGIN_IN_VENV` (the re-exec guard),
and `COGNEE_SYNC_DATASET` / `COGNEE_SYNC_SESSION_ID` (arguments to the final-sync
worker). Setting them yourself does not configure anything — the plugin overwrites
them during startup — and a stale value can misroute identity or session resolution.
Use `COGNEE_SESSION_ID` to pin a session and `COGNEE_PLUGIN_DATASET` to pin a dataset.

It is configured automatically on first launch when no custom status line is already configured. SessionStart writes the correct path into `~/.claude/settings.json` and Claude Code hot-reloads it, so the status line appears from your first interaction onward. Existing non-Cognee `statusLine` settings are preserved; set `COGNEE_STATUSLINE=false` before launching Claude Code to opt out entirely.

The entry sets `refreshInterval: 2`, so Claude re-runs the (network-free, local-only) renderer every 2 seconds in addition to its event-driven updates. Without it, Claude only refreshes the status line on events (a new message, `/compact`, etc.), which go quiet while the session is idle — so a connection change detected right after launch (e.g. a rejected API key) wouldn't show until your next prompt. Tune it with `COGNEE_STATUSLINE_REFRESH_INTERVAL` (seconds; a value below `1`, e.g. `0`, disables idle polling and reverts to event-only refresh).

The status line reads only local state — no network calls on every refresh:
1. Dataset: `COGNEE_PLUGIN_DATASET` env var, otherwise `agent_sessions`
2. Mode: `COGNEE_BASE_URL` env var, then `~/.cognee-plugin/claude-code/config.json` (`base_url`)
3. Default mode: `local`
4. Connection glyph: `conn-state/<session>.json`, then `server-ready.json` + `recall-breaker.json`
5. LLM key: `llm-state/<session>.json`, then `llm-state.json`
6. Counts: `recall/<session>.json`, then `last_recall.json`

## Auto-clear demo hook

For demo flows where each response should clear local transcript context:

```bash
export COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE=true
```

This clears the transcript file on `Stop` after memory capture.

## Logs and state

Claude Code-specific plugin state and logs are written under:

```bash
~/.cognee-plugin/claude-code/
```

Useful logs:

```bash
tail -f ~/.cognee-plugin/claude-code/hook.log
tail -f ~/.cognee-plugin/claude-code/subprocess.log
tail -f ~/.cognee-plugin/claude-code/watcher.log
tail -f ~/.cognee-plugin/claude-code/exit-watcher.log
tail -f ~/.cognee-plugin/claude-code/recall-audit.log
```

Shared state (used by both Claude Code and Codex plugins):

```bash
~/.cognee-plugin/api_key.json     # cached API key
~/.cognee-plugin/venv/            # shared Cognee virtualenv
```

## Usage metrics

For an offline usage rollup compiled purely from the local files above — no
network, no `cognee` import — run:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-plugin" metrics          # readable rollup
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-plugin" metrics --json   # JSON
```

It reports sessions, recalls and hit-rate, saves (prompt/trace/answer), the
local-vs-cloud mode split, and how often an open recall breaker skipped recall.

## Updating

The plugin is versioned with [semver](https://semver.org/) — see
[`CHANGELOG.md`](./CHANGELOG.md). Claude Code offers an update only when the
plugin's `version` changes (it's pinned in the marketplace entry), so a plain
reinstall of the same version reuses the cached copy.

**Update on demand:**

```
/plugin marketplace update cognee     # refresh the marketplace catalog
/plugin update cognee-memory@cognee   # apply a newer version if one exists
```

`/plugin update` reports "already at the latest version" when your installed
version matches the published one.

**Automatic updates (recommended):** Claude Code can refresh marketplaces and
update installed plugins in the background after startup, then prompt you to run
`/reload-plugins`. Auto-update is **enabled by default only for official
Anthropic marketplaces** — for the third-party `cognee` marketplace you must opt
in via Claude Code's per-marketplace auto-update setting (see the "Configure
auto-updates" section of the Claude Code plugin docs). With it on, new releases
land without any manual `/plugin update`.

**If updates still don't appear**, re-add the marketplace source and reinstall:

```
/plugin uninstall cognee-memory@cognee
/plugin marketplace remove cognee
/plugin marketplace add topoteretes/cognee-integrations
/plugin install cognee-memory@cognee
```

### Update notifications

When a newer version is published, the plugin surfaces it automatically — no
configuration needed to receive them:

- **Status line:** an amber `⬆ Cognee update available <installed>→<latest>`
  segment appears, and disappears once you update.
- **SessionStart:** a one-time message per new version, e.g. *"Cognee update
  available 1.0.0 → 1.2.0 — run `/plugin update cognee-memory@cognee`."*

A background check in the idle watcher runs **at most once per day** and fetches a
single public file — the marketplace manifest on the tracked git ref, via
`raw.githubusercontent.com` — to read the published version. It sends no data and
no telemetry, uses a conditional (ETag) request, fails silently when offline, and
skips local-path (dev) installs. Turn it off with `COGNEE_UPDATE_CHECK=false`.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_UPDATE_CHECK` | `true` | Background "update available" check + status-line/SessionStart nudges |
| `COGNEE_UPDATE_CHECK_INTERVAL` | `3600` | Minimum seconds between checks |

## Remove

```
/plugin uninstall cognee-memory@cognee
```

## Troubleshooting

**Recall returns empty but data was ingested**
- Recall is scoped to the active dataset (`COGNEE_PLUGIN_DATASET` / `agent_sessions`).
- Data written via the Python SDK or `client.py` goes to `default_dataset` by default, if dataset not otherwise specified.
- To verify, call the recall API directly without a dataset filter: `curl -X POST "$COGNEE_BASE_URL/api/v1/recall" -d '{"query":"..."}'`

**Session not resolving / wrong session shown**
- Check `~/.cognee-plugin/claude-code/sessions/<host_session_id>.json` — this is the map file for your terminal.
- If it's missing, SessionStart may not have completed; check `~/.cognee-plugin/claude-code/hook.log`.

**Unauthorized / key errors**
- Check `~/.cognee-plugin/api_key.json`. Delete it to force a re-mint.
- Relevant logs: `api_key_cached`, `api_key_minted`, `agent_register_result`.

**Missing session key at startup**
- If the payload session key is missing, SessionStart refuses registration.
- Relevant logs: `session_key_resolved`, `missing_payload_session_id`.

**Final sync diagnostics**
- Check `~/.cognee-plugin/claude-code/hook.log` and `~/.cognee-plugin/claude-code/exit-watcher.log`.
- Relevant logs: `sync_deferred_to_shutdown_worker`, `final_sync_once_*`, `agent_unregister_result`.

## Configuration reference

Config precedence:
1. env vars
2. `~/.cognee-plugin/claude-code/config.json`
3. defaults

| Key | Env var(s) | Default | Notes |
|---|---|---|---|
| `dataset` | `COGNEE_PLUGIN_DATASET` | `agent_sessions` | Dataset for writes and recall (config value is informational-only) |
| `session_id` | `COGNEE_SESSION_ID` | auto-generated per launch | Override to resume a named session |
| `session_strategy` | `COGNEE_SESSION_STRATEGY` | `per-directory` | `per-directory`, `git-branch`, `static` |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `cc` | Prefix for auto-generated session IDs |
| `base_url` | `COGNEE_BASE_URL` | unset | Set to enable managed endpoint mode |
| `api_key` | `COGNEE_API_KEY` | unset | API key; auto-minted if absent in local mode |
| local URL override | `COGNEE_LOCAL_API_URL` | `http://localhost:8011` | Local API base URL |
| local LLM | `LLM_API_KEY`, `LLM_MODEL` | unset | Required for local mode runtime |
| demo auto-clear | `COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE` | disabled | Clear transcript on Stop after capture |
| idle watcher poll | `COGNEE_IDLE_POLL` | `10` | Idle watcher poll interval in seconds |
| idle watcher threshold | `COGNEE_IDLE_THRESHOLD` | `60` | Seconds of inactivity before idle improve fires |
| idle watcher cooldown | `COGNEE_IMPROVE_COOLDOWN` | `600` | Minimum seconds between idle improve runs |
| auto-improve threshold | `COGNEE_AUTO_IMPROVE_EVERY` | `150` | Stored tool calls/stops between automatic improves (0 disables) |
| improve submit timeout | `COGNEE_IMPROVE_SUBMIT_TIMEOUT` | `180` | Read timeout for the improve POST |
| improve poll deadline | `COGNEE_IMPROVE_POLL_DEADLINE` | `600` | Best-effort wait for pipeline completion after submit |
