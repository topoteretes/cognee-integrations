---
name: local-ui
description: Use when launching, checking, or reporting on the local Cognee UI/backend through cognee-cli -ui.
---

# Cognee CLI Local UI

Use this skill when the user asks to launch the local Cognee UI, check whether
Cognee is running, or report how well the UI/backend work.

## Rules

- Use `uv run cognee-cli -ui` as the primary launcher.
- Do not use MCP for this plugin.
- Keep the process running when the user wants the UI available.
- If ports are already occupied, inspect the running services before starting another copy.
- Do not kill user processes without explicit approval.

## Launch

From the Cognee repository root:

```bash
uv run cognee-cli -ui
```

Expected local surfaces:

```text
Backend: http://localhost:8000
Frontend: http://localhost:3000
```

## Health Checks

Backend:

```bash
curl -i http://localhost:8000/health
```

Frontend:

```bash
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

- which process or command is running;
- backend health and any warnings;
- frontend route availability;
- working authenticated flows, if checked;
- broken or suspicious behavior with file references when possible.
