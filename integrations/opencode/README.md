# Cognee Memory Plugin for OpenCode

Gives OpenCode persistent memory across sessions using Cognee's knowledge graph. Tool calls and responses are automatically captured into session memory, relevant context is injected on every compaction, and session data is bridged into the permanent knowledge graph when idle.

## Installation

Add this package to your configuration:

1. Specify `@cognee/cognee-opencode` under the `plugin` array in your `opencode.json` configuration file:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["@cognee/cognee-opencode"]
}
```

2. Make sure you have a running Cognee instance locally (`http://localhost:8000`) or configure environment variables:

```bash
export COGNEE_SERVICE_URL="http://localhost:8000"
export COGNEE_API_KEY="your-api-key" # optional
```

## Modes

The plugin connects to cognee in one of three modes. It picks the mode
automatically from your config:

| Mode | When it's used | How it talks to cognee |
| --- | --- | --- |
| **local-server** (default) | no `COGNEE_BASE_URL`, `COGNEE_EMBEDDED` unset | ensures a local cognee server is running and connects as a thin client |
| **remote** | `COGNEE_BASE_URL` is set | thin client to your managed / cloud cognee |
| **embedded** | `COGNEE_EMBEDDED=true` | runs cognee in-process |

**Why local-server is the default.** cognee's local stores (SQLite, Kuzu/Ladybug,
LanceDB) are single-writer. Driving them in-process from the agent's background
threads — or from a second process sharing the same `data_root` — risks
`database is locked` errors and corruption. A local cognee server is the single
owner that serializes all access, so the agent just makes HTTP calls.
**`embedded` is opt-in and is safe for single-process / offline use only.**

**No silent fallbacks.** The plugin never downgrades modes behind your back. If
`COGNEE_BASE_URL` is set but unreachable, or the local server fails to start,
initialization raises rather than quietly switching to a different mode — silent
fallback would either mask a config error (remote → local data divergence) or
reintroduce the very DB-lock risk this design removes (local-server → embedded).
To accept the single-process trade-off, set `COGNEE_EMBEDDED=true` explicitly.

## Features

- **Auto-capture**: Listens to `tool.execute.after` to store all completed tool execution parameters and outputs directly into Cognee.
- **Auto-recall**: Injects relevant context into the LLM during context compaction using the `experimental.session.compacting` hook.
- **Custom Tools**:
  - `cognee_remember`: Save custom facts, user preferences, or project details into long-term graph memory.
  - `cognee_search`: Search the graph memory for specific details.
