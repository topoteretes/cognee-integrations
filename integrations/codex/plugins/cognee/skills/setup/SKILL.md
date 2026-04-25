---
name: setup
description: Use when setting up, checking, or connecting Cognee through the cognee CLI from Codex.
---

# Cognee CLI Setup

Use this skill when the user asks to configure Cognee, check whether Cognee is
ready, connect to a local or cloud Cognee instance, or inspect CLI capability.

## Rules

- Use `uv run cognee-cli ...` as the primary interface.
- Do not use MCP for this plugin.
- Work from the Cognee repository root when available.
- Do not print secret values from `.env`, config files, shell history, or command output.
- If a command may create, delete, or overwrite durable Cognee state, say what it will affect before running it.

## Checks

Start with the local command surface:

```bash
uv run cognee-cli --help
uv run cognee-cli --version
```

If dependencies are missing, use the repository command:

```bash
uv sync --dev --all-extras --reinstall
```

Inspect configuration without exposing values:

```bash
uv run cognee-cli config list
```

Use `config get <KEY>` only for non-secret settings. For secret-like keys,
report whether the key appears configured rather than showing the value.

## Connect

For a local backend:

```bash
uv run cognee-cli serve --url http://localhost:8000
```

For a hosted instance with an API key, do not paste the key into the transcript.
Use an environment variable or an already configured credential.

To disconnect:

```bash
uv run cognee-cli serve --logout
```

## Multi-Agent Or API Mode

When the user wants concurrent or multi-agent use, prefer a running Cognee API
server and pass:

```bash
uv run cognee-cli --api-url http://localhost:8000 <command>
```

For isolated session history and permissions, include:

```bash
uv run cognee-cli --user-id <uuid> <command>
```
