# Cognee Codex Plugin

Adds persistent Cognee memory to Codex CLI.

The integration:
- captures prompts, tool traces, and assistant responses into session memory
- injects relevant context on prompt submit
- syncs session memory into graph memory on session end/final exit

## Install

Install via the Codex marketplace. First enable hooks, then run the install commands in your terminal or directly inside a Codex session.

You can enable hooks with:

```bash
codex features enable hooks
```

Or set it manually in your Codex config:

```toml
# ~/.codex/config.toml
[features]
hooks = true
```

```bash
codex plugin marketplace add topoteretes/cognee-integrations --ref main
codex plugin add cognee@cognee
```

Then configure your runtime mode — **once** — in `~/.cognee/.env`. The file is created with a commented template on the first session start; values in it act exactly like shell exports (a real `export` in your shell still overrides the file, per terminal). It is shared with the Claude Code plugin, so both read the same configuration. Lines may optionally start with `export `, so existing export lines can be pasted verbatim. Pick one of the two modes below — or configure **both** and flip a terminal with a single export (see [Which mode wins, and how to switch](#which-mode-wins-and-how-to-switch)).

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

**Windows (PowerShell)** — same idea, same file:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.cognee" | Out-Null
@'
COGNEE_BASE_URL="https://your-instance.cognee.ai"
COGNEE_API_KEY="ck_..."
'@ | Add-Content "$env:USERPROFILE\.cognee\.env"
```

Re-running any of these blocks is safe: when a key appears more than once, the **last value wins**, so pasting again with a new value updates the configuration. Editing the file directly (`nano ~/.cognee/.env`) works too. Changes apply on the next session launch. Plain shell `export`s in the launching terminal still take precedence over `~/.cognee/.env` — useful to override the shared config for one terminal. See [Managing the env file](#managing-the-env-file) for the file format and how to add, change, or remove variables later.

### Which mode wins, and how to switch

You can configure **both modes at once** — keep `COGNEE_BASE_URL` + `COGNEE_API_KEY` *and* `LLM_API_KEY` in the file together. The mode is then decided per terminal, by three rules in order:

1. **A `COGNEE_BACKEND` export wins.** `export COGNEE_BACKEND=local` (or `=cloud`) pins that terminal to that mode. Nothing else needs to change.
2. **Otherwise, cloud wins when configured.** If `COGNEE_BASE_URL` is set (in the file or the shell), the plugin connects to it.
3. **Otherwise, local.** With no URL anywhere, the plugin boots the local server.

So with both modes in `~/.cognee/.env`:

```bash
codex                           # → cloud (the configured URL routes)
COGNEE_BACKEND=local codex      # → local, this launch only
export COGNEE_BACKEND=local     # → local for every launch from this shell
```

Details worth knowing:

- **The switch is pinned.** `COGNEE_BACKEND=cloud` with no `COGNEE_BASE_URL` configured still counts as cloud: the plugin does **not** silently fall back to local, and the status line shows `✕ (missing_cognee_base_url)` so you know exactly what to fix.
- **To go local, use the switch — not `unset COGNEE_BASE_URL`.** Unsetting doesn't work: the env file re-injects the URL at the next launch. (Deleting the line from the file works, but that changes the default for *every* terminal.)
- The shared `COGNEE_BACKEND` flips both the Codex **and** Claude Code plugins in that terminal. To flip only one, use `COGNEE_CODEX_BACKEND` / `COGNEE_CLAUDE_BACKEND` — the plugin-specific name beats the shared one.
- Accepted values: `local` (aliases: `native`, `sdk`) and `cloud` (aliases: `http`, `api`, `server`). Anything else is ignored.
- `COGNEE_BACKEND` can also live in `~/.cognee/.env` to make a mode the durable default; a shell export still overrides it per terminal.
- Not sure what a terminal resolved? The status line's mode field shows it live, and `doctor.py` prints the decision with its cause, e.g. `Mode: Local — forced by COGNEE_BACKEND=local`.

You can also set config in `~/.cognee-plugin/config.json`:

```json
{
  "base_url": "https://your-instance.cognee.ai",
  "dataset": "agent_sessions"
}
```

On startup the statusline shows `cognee: <dataset> · local` (or `· cloud`) to confirm the plugin is active.

## Auth

The integration uses a **single auth principal** — one API key, one user. No per-agent credentials.

Key resolution order:
1. `COGNEE_API_KEY` env var
2. `~/.cognee-plugin/api_key.json` (cached from a previous mint)
3. Auto-mint from the default local user (local mode only), then cache to `api_key.json`

## Mode selection rules

At startup (`SessionStart`):
- `COGNEE_BACKEND` (or `COGNEE_CODEX_BACKEND`) exported → that mode, pinned — see [Which mode wins](#which-mode-wins-and-how-to-switch)
- otherwise `COGNEE_BASE_URL` set → `managed_endpoint`
- otherwise → `integration_local` (local API bootstrap)

A forced-local switch also scrubs `COGNEE_BASE_URL`/`COGNEE_API_KEY` from the process environment, so the per-prompt recall/remember calls and every spawned worker resolve the same local endpoint — not just `SessionStart`. A forced-cloud switch with no URL configured never boots the local server; the connection attempt fails visibly instead (status + doctor).

At hook runtime:
- hooks resolve the endpoint from env, then `config.json`, with localhost as the default
- hooks resolve auth from env, then the URL-scoped `api_key.json` cache
- `http` mode skips local SDK initialization

The hooks emit `mode_decision` logs with `mode`, `service_url`, `url_source`, `key_source`, `api_key_present`.

## Sessions

Each terminal launch maintains a small map file:

```
~/.cognee-plugin/sessions/<host_session_id>.json
  → { "conn_uuid": "...", "session_id": "...", "host_key": "..." }
```

- **`session_id`** — which Cognee session this terminal writes to and recalls from. Fixed at launch.
- **`conn_uuid`** — per-launch liveness handle used for agent registration and server shutdown counting.

By default a new `session_id` is generated each launch. Set `COGNEE_SESSION_ID` to resume a specific session:

```bash
export COGNEE_SESSION_ID="my-project"
codex
```

Two terminals can deliberately share a session by setting the same `COGNEE_SESSION_ID`.

## Dataset

All writes and recall are scoped to a single dataset. By default both the Claude Code and Codex plugins use `agent_sessions`, so memory is shared across both integrations automatically.

Set a custom dataset at launch:

```bash
export COGNEE_PLUGIN_DATASET="my-project-memory"
codex
```

`~/.cognee-plugin/config.json` may still show a `dataset` value for visibility,
but runtime dataset selection does not read it.

`COGNEE_PLUGIN_DATASET` seeds the dataset at launch. Recall searches only the active dataset.
Data added outside of Codex to the dataset (via SDK or the server for example) is visible in Codex via the Cognee plugin.

### Switching datasets mid-session

Ask Codex to switch datasets (the `cognee-switch-datasets` skill). Without a name it lists the
datasets you can write to — those owned by the principal behind your API key; datasets you can
only read are counted but never offered — as a numbered list and asks you to pick. A name that
is not listed is created for you.

A Cognee session never spans two datasets, so the switch:

1. syncs the current session into its dataset (aborts if that fails — nothing changes);
2. registers a **new** Cognee session on the chosen dataset under a fresh connection handle,
   then releases the old handle (register-then-unregister, so a local agent-mode server never
   sees zero connections);
3. repoints this launch's record so every hook, the shell wrappers, the idle/exit watchers and
   the in-context status line follow it (it gains a `· switched` tag on the next prompt).

The choice lives in the launch record (`~/.cognee-plugin/codex/sessions/<host id>.json`), so it
survives a resume and beats the shell's `COGNEE_PLUGIN_DATASET` (and a pinned
`COGNEE_SESSION_ID`) for the rest of the launch. Retired sessions stay in the record's `touched`
list and the session-end sync covers them again as a safety net. The script behind the skill is
`scripts/switch-dataset.py` (`--list [--json]`, `<name> [--force] [--json]`,
`--session-key <host id>` when several launches share a directory).

## Hooks

| Hook | Behavior |
|---|---|
| `SessionStart` | mode select, identity setup, dataset readiness, watcher bootstrap |
| `UserPromptSubmit` | context lookup + async prompt staging |
| `PostToolUse` | async trace write |
| `Stop` | assistant answer write |
| `PreCompact` | memory anchor build before compaction |
| `SessionEnd` | trigger detached final sync worker |

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
| `COGNEE_IMPROVE_BUSY_DEADLINE` | `600` | How long to wait for a concurrent improve's session lock before giving up |
| `COGNEE_IMPROVE_BUSY_RETRY_INTERVAL` | `15` | Seconds between re-submits while the session lock is held |

Final sync on session end is triggered by the `SessionEnd` detached worker, with an exit watcher as fallback if the process exits without firing `SessionEnd`.

## Status visibility

Cognee status is shown as `cognee: <dataset> · <mode>`, for example:

```
cognee: agent_sessions · local
cognee: my-project · cloud
```

`<dataset>` is the active Cognee dataset. `<mode>` is `local` when no `COGNEE_BASE_URL` is set or when it points to localhost, and `cloud` when it points to a remote host; an exported `COGNEE_BACKEND` / `COGNEE_CODEX_BACKEND` switch overrides that, so the status always shows the mode the terminal actually resolved.

A connection glyph precedes the line: `●` once the server is confirmed up **and** authenticated, or `✕ (<reason>)` on failure — `incorrect_cognee_api_key` (a missing, wrong, or expired `COGNEE_API_KEY`), `unreachable` (server positively absent: connection refused or DNS failure, including a server that dies mid-session), `server_error` (5xx), `not_responding` (the server accepts connections but hasn't answered for several consecutive prompts — a single slow response never triggers it, so a busy server is not misreported as unreachable), or `missing_cognee_base_url` (the terminal was pinned to cloud with the `COGNEE_BACKEND` switch but no `COGNEE_BASE_URL` is configured anywhere — a misconfiguration proven directly from the environment and shown immediately, not after a failed connection attempt). The state is recorded by the hooks that already talk to the server (SessionStart, and the per-prompt recall), so it stays green until a failure is actually observed and clears back to `●` on the next success. Read from local state only — no network on refresh.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_READY_PROBE_TIMEOUT` | `1.0` | Seconds the per-prompt readiness probe waits before giving up and skipping recall for that turn. It sits on the keystroke→answer path, so the default is deliberately tight; raise it on a slow or loaded server that is otherwise healthy. |

In local mode the plugin also surfaces `LLM_API_KEY` problems (the key the local server uses to call the LLM) **in that same leading glyph slot**: `✕ (incorrect_llm_api_key) cognee: … · local` when the key is missing or the provider rejects it — one reason for both, since the fix is the same either way (`llm-state.json` still records which it was). The slot holds one sign, by precedence: a server-connection failure wins (if the server can't be reached or authenticated, its LLM key isn't the actionable problem), otherwise an LLM-key failure is shown **in place of** the `●` — the `llm_*` reason already tells you the server side itself is fine, so `●` and `✕` never appear together.

Both verdicts come from a single authority: the background idle watcher (off the prompt path). It resolves the key exactly as the server does — Cognee's own config, so a key in `LLM_API_KEY`, a `.env`, or Cognee's config file all count — and validates it with one tiny `max_tokens=1` call through the same LLM stack Cognee uses, making it **provider-agnostic**. Only `401`/`403` counts as a key failure: providers authenticate before validating anything else, so any other response (including the `400` reasoning models return when one token is too few to finish a message) proves the key works, while a transport failure with no HTTP status is inconclusive and leaves the previous verdict alone. It runs once per idle-watcher launch — at session start, and again on any prompt that finds no live watcher — never more often than once per `COGNEE_LLM_CHECK_INTERVAL` seconds (default 300); there is no periodic timer. The verdict clears once the key checks out and expires after 30 minutes, so one left behind by an ended session never lingers.

**Per-terminal status.** Every signal answers *for this session*, not for the machine — terminals legitimately disagree (one shell exported `LLM_API_KEY`, another didn't; two hold different `COGNEE_API_KEY`s). Each writer keeps a machine-wide marker (`server-ready.json`, `llm-state.json`) as **coordination** state — it gates recall and is shared with the Claude Code plugin, since both talk to one server — plus a per-session copy under `conn-state/<session_key>.json` and `llm-state/<session_key>.json` as the **display** state the status reads. Your own record wins, except that a fresher **server-wide** failure in the shared marker takes precedence — `unreachable` or `server_error`, since the server is shared. `incorrect_cognee_api_key` is not propagated: it describes the other session's credential, not the server. A fresher shared `ready` does not clear your own failure either. With no record of your own, the shared marker counts only when unattributed; another session's record is ignored and no glyph is shown. Local mode only; disable with `COGNEE_LLM_KEY_CHECK=false`.

**Internal variables — do not set these.** A few `COGNEE_*` names in the environment
are the plugin's own inter-process plumbing, written by one hook and read back by the
detached workers it spawns: `COGNEE_USER_ID` (the resolved Cognee user for this
launch), `COGNEE_SESSION_KEY` (the host session key every hook of a launch resolves
through), `COGNEE_AGENT_SESSION_NAME`, `COGNEE_PLUGIN_IN_VENV` (the re-exec guard),
and `COGNEE_SYNC_DATASET` / `COGNEE_SYNC_SESSION_ID` (arguments to the final-sync
worker). Setting them yourself does not configure anything — the plugin overwrites
them during startup — and a stale value can misroute identity or session resolution.
Use `COGNEE_SESSION_ID` to pin a session and `COGNEE_PLUGIN_DATASET` to seed the dataset
(a mid-session dataset switch overrides both for that launch).

The renderer reads only local state — no network calls on every refresh:
1. Dataset: this launch's record (`sessions/<host id>.json`, written at SessionStart and by a dataset switch), otherwise `COGNEE_PLUGIN_DATASET`, otherwise `agent_sessions`
2. Mode: `COGNEE_BACKEND` / `COGNEE_CODEX_BACKEND` switch, then `COGNEE_BASE_URL` env var, then `~/.cognee-plugin/config.json` (`base_url`)
3. Default mode: `local`

## Logs and state

Plugin state and logs are written under:

```bash
~/.cognee-plugin/codex/
```

Useful logs:

```bash
tail -f ~/.cognee-plugin/codex/hook.log
tail -f ~/.cognee-plugin/codex/subprocess.log
tail -f ~/.cognee-plugin/codex/recall-audit.log
tail -f ~/.cognee-plugin/codex/exit-watcher.log
tail -f ~/.cognee-plugin/codex/watcher.log
```

## Usage metrics

For an offline usage rollup compiled purely from the local files above — no
network, no `cognee` import — run:

```bash
python3 "${PLUGIN_ROOT}/scripts/cognee-plugin" metrics          # readable rollup
python3 "${PLUGIN_ROOT}/scripts/cognee-plugin" metrics --json   # JSON
```

It reports sessions, recalls and hit-rate, saves (prompt/trace/answer), the
local-vs-cloud mode split, and how often an open recall breaker skipped recall.

## Updating

The `cognee` marketplace tracks the repository's `main` branch (`git-subdir`,
`ref: main`), so updates arrive as new commits — they are **not** gated by the
plugin `version` field. Pull the latest with:

```bash
codex plugin marketplace upgrade cognee
```

`marketplace upgrade` resolves `main` to its current commit and force-reinstalls
when it has moved; if nothing changed it reports no upgrade. Note there is **no
per-plugin `codex plugin update`, and no automatic background updates** for
user-added marketplaces — run `upgrade` when you want the latest.

The `version` in `.codex-plugin/plugin.json` (see
[`CHANGELOG.md`](./plugins/cognee/CHANGELOG.md)) follows semver and is bumped each
release. It is the cache key and lets a normal load reinstall when it changes,
but the commit ref above is what actually drives updates.

If a stale cached copy persists, remove and re-add:

```bash
codex plugin remove cognee@cognee
codex plugin add cognee@cognee
```

### Update notifications

When a newer version is published, the plugin surfaces it automatically:

- **In-context status:** a short `⬆ Cognee update available <installed>→<latest>`
  segment appears in Cognee's status line (which Codex injects into the model's
  context) and disappears once you update.
- **SessionStart:** a one-time note per new version — *"Cognee update available
  1.0.3 → 1.1.0 — run `codex plugin marketplace upgrade cognee`."*

A background check in the idle watcher runs **at most once per day** and fetches a
single public file — the plugin manifest on `main`, via `raw.githubusercontent.com`
— to read the published version. It sends no data and no telemetry, uses a
conditional (ETag) request, and fails silently when offline. Because Codex tracks
`main`, the nudge fires on version bumps (releases), not on every commit. Turn it
off with `COGNEE_UPDATE_CHECK=false`.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_UPDATE_CHECK` | `true` | Background "update available" check + status/SessionStart nudges |
| `COGNEE_UPDATE_CHECK_INTERVAL` | `3600` | Minimum seconds between checks |

## Remove

```bash
codex plugin remove cognee@cognee
codex plugin marketplace remove cognee
```

## Configuration reference

Config precedence:
1. env vars (shell exports)
2. `~/.cognee/.env` (one-time setup file, shared with the Claude Code plugin; loaded into the environment at process start, so every env var below can live here)
3. `~/.cognee-plugin/config.json`
4. defaults

One exception sits above all four layers: the `COGNEE_BACKEND` / `COGNEE_CODEX_BACKEND` mode switch. When exported, it pins the mode regardless of where the connection variables are defined — forced local ignores a configured `COGNEE_BASE_URL` entirely (it is scrubbed from the process environment), and forced cloud stays cloud even when the URL is missing. See [Which mode wins](#which-mode-wins-and-how-to-switch).

`~/.cognee/.env` is created with a commented template on first session start (permissions `0600`; the path can be overridden with `COGNEE_ENV_FILE`). Run `doctor.py` to see which keys the file defines, which are overridden by shell exports, and — in the mode row — whether a backend switch forced the mode decision.

### Managing the env file

`~/.cognee/.env` is a plain dotenv text file. Every env var in the table below can live in it, and you can manage it entirely from the command line — or open it in any editor.

**Add or change a variable without an editor** — append it. When a key appears more than once, the **last value wins**, so appending the same key again with a new value is also how you *change* it:

```bash
echo 'COGNEE_PLUGIN_DATASET="my-project-memory"' >> ~/.cognee/.env
```

```powershell
Add-Content "$env:USERPROFILE\.cognee\.env" 'COGNEE_PLUGIN_DATASET="my-project-memory"'
```

For secrets (`COGNEE_API_KEY`, `LLM_API_KEY`), prefer the editor route below — an `echo` puts the key into your shell history.

**Edit the file manually** — open it in any editor:

```bash
nano ~/.cognee/.env          # or vim, code, open -e (macOS)
```

```powershell
notepad "$env:USERPROFILE\.cognee\.env"
```

Since the file starts as a commented template, editing usually means uncommenting a line and filling in your value. The format:

```bash
# Comments start with '#'; blank lines are ignored.
COGNEE_BASE_URL="https://your-instance.cognee.ai"
COGNEE_API_KEY=ck_abc123                # quotes are optional
export LLM_API_KEY="sk-..."             # a leading 'export ' is tolerated, so
                                        # shell profile lines paste verbatim
```

Keys are letters, digits, and underscores. Values are taken literally — no `$VAR` interpolation, no multi-line values. Malformed lines are skipped silently, never fatal, and process-critical variables (`PATH` and friends) are ignored by design.

**Remove a variable** — delete (or comment out) its line in the editor. To switch modes you usually don't need to remove anything: keep both modes' variables in the file and export the switch instead — `export COGNEE_BACKEND=local` (see [Which mode wins](#which-mode-wins-and-how-to-switch)). Remove the `COGNEE_BASE_URL` line only when you want local to become the permanent default for every terminal.

**Apply and verify** — the file is read at session start, so changes take effect on the next `codex` launch. If a value seems to be ignored, check whether the same variable is `export`ed in your shell: real exports always win over the file. The doctor's **Env File** row lists which keys the file defines and flags any that a shell export is overriding.

| Key | Env var(s) | Default | Notes |
|---|---|---|---|
| `dataset` | `COGNEE_PLUGIN_DATASET` | `agent_sessions` | Dataset for writes and recall at launch; the `cognee-switch-datasets` skill changes it mid-session (config value is informational-only) |
| `session_id` | `COGNEE_SESSION_ID` | auto-generated per launch | Override to resume a named session |
| `session_strategy` | `COGNEE_SESSION_STRATEGY` | `per-directory` | `per-directory`, `git-branch`, `static` |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `codex` | Prefix for auto-generated session IDs |
| `base_url` | `COGNEE_BASE_URL` | unset | Set to enable managed endpoint mode |
| `api_key` | `COGNEE_API_KEY` | unset | API key; auto-minted if absent in local mode |
| mode switch | `COGNEE_BACKEND` | unset | `local` or `cloud` — pins the terminal's mode, overriding the URL rule; flips the Codex **and** Claude Code plugins |
| plugin-only mode switch | `COGNEE_CODEX_BACKEND` | unset | Same, for this plugin only; beats `COGNEE_BACKEND` |
| local URL override | `COGNEE_LOCAL_API_URL` | `http://localhost:8011` | Local API base URL |
| local LLM | `LLM_API_KEY`, `LLM_MODEL` | unset | Required for local mode runtime |
| idle watcher poll | `COGNEE_IDLE_POLL` | `10` | Idle watcher poll interval in seconds |
| idle watcher threshold | `COGNEE_IDLE_THRESHOLD` | `60` | Seconds of inactivity before idle improve fires |
| idle watcher cooldown | `COGNEE_IMPROVE_COOLDOWN` | `600` | Minimum seconds between idle improve runs |
| auto-improve threshold | `COGNEE_AUTO_IMPROVE_EVERY` | `150` | Stored tool calls/stops between automatic improves (0 disables) |
| improve submit timeout | `COGNEE_IMPROVE_SUBMIT_TIMEOUT` | `180` | Read timeout for the improve POST |

## Troubleshooting

**Terminal connects to cloud when you wanted local (or the reverse)**
- The mode is routed by `COGNEE_BASE_URL`: configured anywhere (env file or shell) → cloud; otherwise → local. An exported `COGNEE_BACKEND=local` / `=cloud` overrides that for the terminal — see [Which mode wins](#which-mode-wins-and-how-to-switch).
- `unset COGNEE_BASE_URL` does **not** go local: the env file re-injects the URL at the next launch. Export `COGNEE_BACKEND=local` instead.
- Check what the terminal actually resolved: the status's mode field shows it live, and `doctor.py` prints the decision with its cause (`Mode: Local — forced by COGNEE_BACKEND=local`).
- If a mode seems stuck, check for a forgotten `COGNEE_BACKEND` / `COGNEE_CODEX_BACKEND` export in the shell or in `~/.cognee/.env` — the plugin-specific name silently beats the shared one.

**Recall returns empty but data was ingested**
- Recall is scoped to the active dataset (the one in the status line — `COGNEE_PLUGIN_DATASET` / `agent_sessions` at launch, or whatever you switched to).
- Data written via the Python SDK or `client.py` goes to `default_dataset` by default, if dataset not otherwise specified.
- To verify, call the recall API directly without a dataset filter: `curl -X POST "$COGNEE_BASE_URL/api/v1/recall" -d '{"query":"..."}'`

**SessionStart hook invalid JSON output**
- Check `hook.log` and confirm the installed plugin version matches the expected hook contract.

**No new behavior after local edits**
- Codex may still be running a cached Git marketplace copy. Confirm installed marketplace/plugin source, then reinstall from the intended source.

**Startup / local endpoint issues**

```bash
tail -f ~/.cognee-plugin/codex/hook.log
tail -f ~/.cognee-plugin/codex/subprocess.log
curl -sS http://localhost:8011/health
```

**Unauthorized / key errors**
- Check `~/.cognee-plugin/api_key.json`. Delete it to force a re-mint.
- Relevant logs: `api_key_cached`, `api_key_minted`, `agent_register_result`.

**Missing session key at startup**
- If the payload session key is missing, SessionStart refuses registration.
- Relevant logs: `session_key_resolved`, `missing_payload_session_id`.

**Final sync diagnostics**
- Check `~/.cognee-plugin/codex/hook.log` and `~/.cognee-plugin/codex/exit-watcher.log`.
- Relevant logs: `sync_deferred_to_shutdown_worker`, `final_sync_once_*`, `agent_unregister_result`.
