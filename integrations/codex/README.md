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

Then set environment variables for your runtime mode.

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
- `COGNEE_BASE_URL` set → `managed_endpoint`
- otherwise → `integration_local` (local API bootstrap)

At hook runtime:
- hooks resolve mode through runtime endpoint auth (env + `api_key.json`), not only config intent
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

The dataset is fixed for the lifetime of a launch. Recall searches only the active dataset. If you want to
change the active dataset, you have to exit Claude, change the dataset via env, and then start Claude again.
Data added outside of Claude to the dataset (via SDK or the server for example) is visible in Claude via the Cognee plugin.

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

`<dataset>` is the active Cognee dataset. `<mode>` is `local` when no `COGNEE_BASE_URL` is set or when it points to localhost, and `cloud` when it points to a remote host.

The renderer reads only local state — no network calls on every refresh:
1. Dataset: `COGNEE_PLUGIN_DATASET` env var, otherwise `agent_sessions`
2. Mode: `COGNEE_BASE_URL` env var, then `~/.cognee-plugin/config.json` (`base_url`)
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
| `COGNEE_UPDATE_CHECK_INTERVAL` | `86400` | Minimum seconds between checks |

## Remove

```bash
codex plugin remove cognee@cognee
codex plugin marketplace remove cognee
```

## Configuration reference

Config precedence:
1. env vars
2. `~/.cognee-plugin/config.json`
3. defaults

| Key | Env var(s) | Default | Notes |
|---|---|---|---|
| `dataset` | `COGNEE_PLUGIN_DATASET` | `agent_sessions` | Dataset for writes and recall (config value is informational-only) |
| `session_id` | `COGNEE_SESSION_ID` | auto-generated per launch | Override to resume a named session |
| `session_strategy` | `COGNEE_SESSION_STRATEGY` | `per-directory` | `per-directory`, `git-branch`, `static` |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `codex` | Prefix for auto-generated session IDs |
| `base_url` | `COGNEE_BASE_URL` | unset | Set to enable managed endpoint mode |
| `api_key` | `COGNEE_API_KEY` | unset | API key; auto-minted if absent in local mode |
| local URL override | `COGNEE_LOCAL_API_URL` | `http://localhost:8011` | Local API base URL |
| local LLM | `LLM_API_KEY`, `LLM_MODEL` | unset | Required for local mode runtime |
| idle watcher poll | `COGNEE_IDLE_POLL` | `10` | Idle watcher poll interval in seconds |
| idle watcher threshold | `COGNEE_IDLE_THRESHOLD` | `60` | Seconds of inactivity before idle improve fires |
| idle watcher cooldown | `COGNEE_IMPROVE_COOLDOWN` | `600` | Minimum seconds between idle improve runs |
| auto-improve threshold | `COGNEE_AUTO_IMPROVE_EVERY` | `150` | Stored tool calls/stops between automatic improves (0 disables) |
| improve submit timeout | `COGNEE_IMPROVE_SUBMIT_TIMEOUT` | `180` | Read timeout for the improve POST |

### Per-operation timeouts

Each operation has its own client timeout, tunable independently (all in seconds):

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_RECALL_TIMEOUT` | `20` | Client timeout for a recall request |
| `COGNEE_REMEMBER_TIMEOUT` | `60` | Client timeout for remember writes (submit POST and per-turn entry store) |
| `COGNEE_REGISTER_TIMEOUT` | `15` | Client timeout for the session register call |

`COGNEE_REMEMBER_TIMEOUT` also caps the per-turn session-entry write (`/api/v1/remember/entry`); when the variable is unset that path uses 30s, while the explicit remember submit uses 60s.

## Troubleshooting

**Recall returns empty but data was ingested**
- Recall is scoped to the active dataset (`COGNEE_PLUGIN_DATASET` / `agent_sessions`).
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
