# Cognee inspector

A small local page for watching what the Claude Code plugin and the Cognee server do
while you test: one timeline, newest first, refreshed every 2.5 seconds.

```bash
python3 integrations/claude-code/tools/inspector/inspector.py
# open http://127.0.0.1:8030
```

It merges four sources:

| Source | What it shows | Where it comes from |
|---|---|---|
| Plugin hooks | every event the plugin logs (prompt, recall, capture, lessons, review) | `~/.cognee-plugin/claude-code/hook.log` |
| Server runs | pipeline and operation runs; an improve run expands to its stages | `GET /api/v1/activity/pipeline-runs` |
| enola | each enola command the server ran | the server log |
| Server log | improve, distillation and code-graph lines, and errors | the server log |

Every row shows the session it belongs to. Click a session id to show only that
session, click a row to expand its details. "Hide routine events" drops the plugin's
bookkeeping events; "Errors only" keeps failures.

## What it does not do

- It is read-only: it changes no plugin state and writes nothing to the server.
- It shows events and runs, not each raw HTTP request and response.
- It reads the Claude Code plugin's log only, and about the last 600 KB of each log.
- Improve stages appear only on a server that stores them on the run row.

## Settings

| Variable | Default |
|---|---|
| `INSPECTOR_PORT` | `8030` |
| `INSPECTOR_SERVER_URL` | the base URL in `~/.cognee-plugin/api_key.json`, else `http://localhost:8011` |
| `INSPECTOR_SERVER_LOG` | `~/.cognee-plugin/claude-code/server-console.log` (the server the plugin starts) |
| `INSPECTOR_HOOK_LOG` | `~/.cognee-plugin/claude-code/hook.log` |

It binds to `127.0.0.1`, uses the plugin's cached API key only to ask the server for
its runs, and replaces the value of any key named like a secret (`api_key`, `token`,
`password`, ...) in event details before showing them. It uses the Python standard
library only.
