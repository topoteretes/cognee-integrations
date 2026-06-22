# Cognee Memory Plugin for Claude Code

Adds persistent memory to Claude Code through Cognee.

The integration:
- captures prompts, tool traces, and assistant responses into session memory
- injects relevant context on prompt submit
- syncs session memory into graph memory on session end/final exit

## Install

Install via the Claude Code marketplace, then set environment variables for your runtime mode.

**Cognee Cloud or a remote server** — set both:

```bash
export COGNEE_SERVICE_URL="https://your-instance.cognee.ai"
export COGNEE_API_KEY="ck_..."
```

**Local mode** (default when `COGNEE_SERVICE_URL` is not set) — the plugin bootstraps a local Cognee API at `http://localhost:8011`. Only `LLM_API_KEY` is required; `COGNEE_API_KEY` is auto-minted if absent:

```bash
export LLM_API_KEY="sk-..."
```

You can also set config in `~/.cognee-plugin/config.json`:

```json
{
  "service_url": "https://your-instance.cognee.ai",
  "dataset": "claude_sessions"
}
```

On startup you should see a "Cognee Memory Connected" system message.

## Auth

The integration uses a **single auth principal** — one API key, one user. No per-agent credentials.

Key resolution order:
1. `COGNEE_API_KEY` env var
2. `~/.cognee-plugin/api_key.json` (cached from a previous mint)
3. Auto-mint from the default local user (local mode only), then cache to `api_key.json`

## Mode selection rules

At startup (`SessionStart`):
- `COGNEE_SERVICE_URL` set → `managed_endpoint`
- otherwise → `integration_local` (local API bootstrap)

At hook runtime:
- hooks resolve mode through runtime endpoint auth (env + `api_key.json`), not only config intent
- `http` mode skips local SDK initialization
- `local_sdk` mode runs `ensure_cognee_ready(...)`

The hooks emit `mode_decision` logs with `mode`, `service_url`, `url_source`, `key_source`, `api_key_present`.

## Sessions

Each terminal launch maintains a small map file:

```
~/.cognee-plugin/sessions/<host_session_id>.json
  → { "conn_uuid": "...", "session_id": "...", "host_key": "...", "touched": [...] }
```

- **`session_id`** — which Cognee session this terminal writes to and recalls from. Switchable without restarting.
- **`conn_uuid`** — per-launch liveness handle used for agent registration and server shutdown counting. Never changes mid-launch.

By default a new `session_id` is generated each launch. Set `COGNEE_SESSION_ID` to resume a specific session:

```bash
export COGNEE_SESSION_ID="my-project"
```

Two terminals can deliberately share a session by setting the same `COGNEE_SESSION_ID`.

### Session switching

Use the `/cognee-configure-session` skill to list and switch sessions without restarting:

```
/cognee-configure-session
```

This lists all Cognee sessions, shows which one is current, and prompts you to pick one. When you switch, the outgoing session is flushed to the graph before the switch commits. The new session takes effect immediately — recall and saving happen in the new session from the next message on.

The switch affects only the current terminal. Other running agents keep their own sessions.

## Hooks

| Hook | Behavior |
|---|---|
| `SessionStart` | mode select, identity setup, dataset readiness, watcher bootstrap |
| `UserPromptSubmit` | context lookup + async prompt staging |
| `PostToolUse` | async trace write |
| `Stop` | assistant answer write + optional transcript clear hook |
| `PreCompact` | memory anchor build before compaction |
| `SessionEnd` | trigger detached final sync worker |

Claude-specific contracts are preserved:
- `hookSpecificOutput` payload format
- async hook behavior for write hooks

## Session sync and watchers

Final sync can be triggered by:
- `SessionEnd` detached worker path
- exit watcher fallback when process exits

To avoid duplicate final sync:
- detached workers claim one-shot markers in `~/.cognee-plugin/final-sync-once/*.done`
- stale markers are pruned with TTL of 1 hour

Final detached sync also performs unregister-on-finish when applicable.

## Skills

- `/cognee-memory:cognee-remember`
- `/cognee-memory:cognee-search`
- `/cognee-memory:cognee-sync`
- `/cognee-memory:cognee-configure-session`

## Status line (optional)

The status line displays `cognee: <session-id> (+N more)` for the current terminal's active session.

Enable it by running the setup script once after installing the plugin:

```bash
python3 ~/.claude/plugins/marketplaces/cognee/integrations/claude-code/scripts/setup-statusline.py
```

This writes the correct absolute path into `~/.claude/settings.json` automatically and preserves all other settings. Restart Claude Code after running it.

The status line reads only local files — no network calls on every refresh:
- `~/.cognee-plugin/sessions/<host_id>.json` — current session for this terminal
- `~/.cognee-plugin/sessions_count.json` — total session count (TTL-refreshed by the plugin)

Shows `cognee: starting...` until SessionStart completes.

## Auto-clear demo hook

For demo flows where each response should clear local transcript context:

```bash
export COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE=true
```

This clears the transcript file on `Stop` after memory capture.

## Breaking changes and migration notes

- `agent_keys.json` and per-agent credentials are removed. The single key is now cached at `~/.cognee-plugin/api_key.json`.
- `COGNEE_AGENT_NAME`, `COGNEE_USER_EMAIL`, `COGNEE_USER_PASSWORD` are no longer used.
- Session IDs are now scoped per-launch via map files under `~/.cognee-plugin/sessions/`. The `COGNEE_SESSION_ID` env var is the primary override (not legacy).
- `resolved.json` is no longer used.
- Hook-time routing is runtime-auth driven (`http` vs `local_sdk`).
- Session-end sync uses detached workers + dedupe markers.

## Troubleshooting

**Session not resolving / wrong session shown**
- Check `~/.cognee-plugin/sessions/<host_session_id>.json` — this is the map file for your terminal.
- If it's missing, SessionStart may not have completed; check `~/.cognee-plugin/hook.log`.

**Picker reports "could not determine current launch"**
- The session-set script walks the process tree to find the host pid and reads `~/.cognee-plugin/launches/<host_pid>.json`.
- Check that `launches/` contains a file for the current Claude pid. Missing bridge = SessionStart didn't complete.

**Unauthorized / key errors**
- Check `~/.cognee-plugin/api_key.json`. Delete it to force a re-mint.
- Relevant logs: `api_key_cached`, `api_key_minted`, `agent_register_result`.

**Missing session key at startup**
- If the payload session key is missing, SessionStart refuses registration.
- Relevant logs: `session_key_resolved`, `missing_payload_session_id`.

**Final sync diagnostics**
- Check `~/.cognee-plugin/hook.log` and `~/.cognee-plugin/exit-watcher.log`.
- Relevant logs: `sync_deferred_to_shutdown_worker`, `final_sync_once_*`, `agent_unregister_result`.

## Configuration reference

Config precedence:
1. env vars
2. `~/.cognee-plugin/config.json`
3. defaults

| Key | Env var(s) | Default | Notes |
|---|---|---|---|
| `dataset` | `COGNEE_CLAUDE_DATASET`, `COGNEE_PLUGIN_DATASET` | `claude_sessions` | Dataset name |
| `session_id` | `COGNEE_SESSION_ID` | auto-generated per launch | Override to resume a named session |
| `session_strategy` | `COGNEE_SESSION_STRATEGY` | `per-directory` | `per-directory`, `git-branch`, `static` |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `cc` | Prefix for auto-generated session IDs |
| `service_url` | `COGNEE_SERVICE_URL` | unset | Set to enable managed endpoint mode |
| `api_key` | `COGNEE_API_KEY` | unset | API key; auto-minted if absent in local mode |
| local URL override | `COGNEE_LOCAL_API_URL` | `http://localhost:8011` | Local API base URL |
| local LLM | `LLM_API_KEY`, `LLM_MODEL` | unset | Required for local mode runtime |
| demo auto-clear | `COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE` | disabled | Clear transcript on Stop after capture |
