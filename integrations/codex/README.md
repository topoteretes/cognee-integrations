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

Or persist it in `~/.cognee-plugin/config.json`:

```json
{ "dataset": "my-project-memory" }
```

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

An idle watcher runs in the background for the lifetime of each launch. It polls activity every `COGNEE_IDLE_POLL` seconds and persists the session cache when the session has been quiet for `COGNEE_IDLE_THRESHOLD` seconds, then waits at least `COGNEE_IMPROVE_COOLDOWN` seconds before the next run.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_IDLE_POLL` | `10` | Poll interval in seconds |
| `COGNEE_IDLE_THRESHOLD` | `60` | Seconds of inactivity before idle sync fires |
| `COGNEE_IMPROVE_COOLDOWN` | `120` | Minimum seconds between idle sync runs |

Final sync on session end is triggered by the `SessionEnd` detached worker, with an exit watcher as fallback if the process exits without firing `SessionEnd`.

## Status visibility

Cognee status is shown as `cognee: <dataset> · <mode>`, for example:

```
cognee: agent_sessions · local
cognee: my-project · cloud
```

`<dataset>` is the active Cognee dataset. `<mode>` is `local` when no `COGNEE_BASE_URL` is set or when it points to localhost, and `cloud` when it points to a remote host.

The renderer reads only local state — no network calls on every refresh:
1. `COGNEE_PLUGIN_DATASET` / `COGNEE_BASE_URL` env vars
2. `~/.cognee-plugin/config.json` → `dataset` and `base_url` keys
3. Default: `cognee: agent_sessions · local`

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

## Update or remove

Reinstall plugin after marketplace/plugin changes:

```bash
codex plugin remove cognee@cognee
codex plugin add cognee@cognee
```

Remove plugin and marketplace:

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
| `dataset` | `COGNEE_PLUGIN_DATASET` | `agent_sessions` | Dataset for writes and recall |
| `session_id` | `COGNEE_SESSION_ID` | auto-generated per launch | Override to resume a named session |
| `session_strategy` | `COGNEE_SESSION_STRATEGY` | `per-directory` | `per-directory`, `git-branch`, `static` |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `codex` | Prefix for auto-generated session IDs |
| `agent_name` | `COGNEE_AGENT_NAME` | `codex-agent` | Agent display name |
| `backend` | `COGNEE_CODEX_BACKEND` | `auto` | Backend routing override |
| `base_url` | `COGNEE_BASE_URL` | unset | Set to enable managed endpoint mode |
| `api_key` | `COGNEE_API_KEY` | unset | API key; auto-minted if absent in local mode |
| `user_email` | `COGNEE_USER_EMAIL` | `default_user@example.com` | Local bootstrap user email |
| `user_password` | `COGNEE_USER_PASSWORD` | `default_password` | Local bootstrap user password |
| local URL override | `COGNEE_LOCAL_API_URL` | `http://localhost:8011` | Local API base URL |
| local LLM | `LLM_API_KEY`, `LLM_MODEL` | unset | Required for local mode runtime |
| idle watcher poll | `COGNEE_IDLE_POLL` | `10` | Idle watcher poll interval in seconds |
| idle watcher threshold | `COGNEE_IDLE_THRESHOLD` | `60` | Seconds of inactivity before idle sync fires |
| idle watcher cooldown | `COGNEE_IMPROVE_COOLDOWN` | `120` | Minimum seconds between idle sync runs |

## Troubleshooting

**Recall returns empty but data was ingested**
- Recall is scoped to the active dataset (`COGNEE_PLUGIN_DATASET` / `config.json` / `agent_sessions`).
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
