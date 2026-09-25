---
name: local-ui
description: Use only when the user explicitly asks to launch or inspect the LOCAL Cognee UI (the cognee-cli dev UI). For "is Cognee running/connected" or any configuration question, use the setup skill instead. Does not apply in cloud mode.
---

# Cognee Local UI

Use this skill only when the user explicitly asks to launch or inspect the
**local** Cognee UI — the development UI that `cognee-cli -ui` serves from a
cognee source checkout.

This skill does **not** answer "is Cognee running?" or "is Cognee connected?".
Those are connection questions: use the **setup** skill, which reads the
plugin's own diagnostic and probes the resolved server.

## Rules

- **Check the mode first.** The local UI exists only in local mode; see step 1.
  Never launch or probe it before confirming the mode.
- **Server first for status.** The plugin's server is the source of truth for
  whether Cognee works. The UI is a viewer on top of it, not the thing to test.
- **`cognee-cli -ui` requires a cognee source checkout.** It is not available on
  a normal plugin install. Establish that prerequisite before attempting it.
- Keep the process running when the user wants the UI available.
- If ports are already occupied, inspect the running services before starting
  another copy.
- Do not kill user processes without explicit approval.
- Do not use MCP for this plugin.

## 1. Check the mode — stop here on cloud

```bash
python3 "${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/doctor.py" --json
```

If `mode` is **`Cloud`**, there is no local UI to launch and nothing here
applies. Tell the user their instance is remote, that its dashboard lives at
their Cognee Cloud URL, and report the connection facts from the same output
(`server_url`, `reachable`, `latency_ms`, `api_key_source`). Do **not** start a
local server or probe localhost — a local UI would be a second, empty instance
holding none of their memory.

If `mode` is **`Local`**, the plugin's own server is already running at
`server_url` (normally `http://localhost:8011`). Confirm the state the user
actually cares about there first:

```bash
curl -sS -i "${COGNEE_BASE_URL:-http://localhost:8011}/health"
```

That is the server the plugin reads and writes. The `cognee-cli -ui` surfaces
below are a **separate** dev instance on different ports — launching them does
not affect, and does not report on, the plugin's memory.

## 2. Launch the dev UI (local mode, cognee checkout required)

Only when the user explicitly wants the UI, and only from a cognee source
checkout:

```bash
"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}/scripts/cognee-cli.sh" -ui
```

The wrapper exits 64 ("Run this from the Cognee repository root or set
COGNEE_REPO_ROOT") when there is no checkout. That is expected on a normal
plugin install — report it as "the local dev UI needs a cognee source checkout",
not as a Cognee failure, and fall back to reporting the plugin server's health
from step 1.

Expected surfaces for that dev instance:

```text
Backend:  http://localhost:8000
Frontend: http://localhost:3000
```

These are `cognee-cli -ui`'s own defaults and are unrelated to the plugin's
`:8011` server — do not use them to judge whether the plugin's memory works.

## 3. Health checks for the dev UI

Only for a UI launched in step 2:

```bash
curl -i http://localhost:8000/health
curl -i http://localhost:3000/
```

Useful route checks:

```bash
curl -i http://localhost:3000/dashboard
curl -i http://localhost:3000/datasets
curl -i http://localhost:3000/search
curl -i http://localhost:3000/knowledge-graph
```

If authenticated checks are needed, use the repository's documented local test
credentials only when appropriate and do not expose real user credentials.

## Reporting Status

Report:

- the resolved **mode** and the plugin server's health first — that is what
  determines whether memory works;
- which process or command is running, if a UI was launched;
- dev-UI backend health and any warnings;
- frontend route availability;
- working authenticated flows, if checked;
- broken or suspicious behavior with file references when possible.

Never present a healthy dev UI as proof that the plugin's memory is working, or
an absent dev UI as proof that it is broken — they are different instances.
