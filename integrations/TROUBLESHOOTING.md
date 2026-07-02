# Cognee Integrations Troubleshooting Guide

This guide covers common failures across the Cognee agent integrations:

- Claude Code: [`integrations/claude-code`](claude-code/README.md)
- Codex: [`integrations/codex`](codex/README.md)
- OpenClaw: [`integrations/openclaw`](openclaw/README.md)
- Hermes Agent: [`integrations/hermes-agent`](hermes-agent/README.md)
- n8n: [`integrations/n8n`](n8n/README.md)

Start with the symptom, then verify the likely cause before changing configuration.

## Quick triage checklist

1. Confirm which backend you are using: local server, embedded/local SDK, remote/self-hosted, or Cognee Cloud.
2. Confirm the base URL and API surface match the integration:
   - Claude Code/Codex local plugin default: `http://localhost:8011`
   - Hermes local-server default: `http://localhost:8000`
   - n8n Add/Cognify/Search/Delete resources: `/api/*`
   - n8n Skill resource and agent plugin endpoints: `/api/v1/*`
3. Check the integration logs before reinstalling or changing code.
4. Confirm the active dataset/session. Most empty-recall reports are scoping or ingestion-state issues.

## Common symptoms

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Local or cloud endpoint is slow or unavailable on the first request | Cold start, sleeping service, or the local Cognee server is still booting | Retry after a short wait and run a health check (`curl -sS <base-url>/health`). For local plugins, check subprocess/server logs and increase the startup timeout if the integration supports one. |
| Search/recall errors mention vector size, dimension mismatch, or incompatible index schema | The stored vector index was created with a different embedding model or embedding dimension than the current configuration | Use one embedding model per dataset/index. Recreate or clear the affected dataset/index after changing embedding providers or dimensions, then ingest and cognify again. |
| Import, syntax, or dependency failures in local mode | Wrong Python/conda environment, commonly Python `<3.10`, or dependencies installed into a different environment than the integration uses | Use the integration's pinned toolchain (`uv sync`, `npm install`, or the documented package install). Confirm `python --version` is `>=3.10` for Python integrations and rerun the integration from that environment. |
| Session is missing from the UI or a different session appears after changing config mid-run | Session mapping is created at process startup; mid-session changes to dataset, session ID, or mode do not rewrite existing host-session mappings | Start a new agent session after changing session/dataset/mode config. For Claude Code/Codex, inspect the session map under `~/.cognee-plugin/.../sessions/`. For Hermes, rerun setup or restart the Hermes process after config changes. |
| Recall returns empty even though data was added | Data has not been cognified/improved yet, was written to a different dataset/session, or graph build is still running in the background | Verify the active dataset/session, then run the integration's sync/improve/cognify step. If writes run in the background, wait for completion and check logs before assuming data was lost. |

## Integration-specific diagnostics

### Claude Code

Useful logs and state:

```bash
tail -f ~/.cognee-plugin/claude-code/hook.log
tail -f ~/.cognee-plugin/claude-code/subprocess.log
tail -f ~/.cognee-plugin/claude-code/watcher.log
tail -f ~/.cognee-plugin/claude-code/exit-watcher.log
tail -f ~/.cognee-plugin/claude-code/recall-audit.log
ls ~/.cognee-plugin/claude-code/sessions/
```

Checks:

- Confirm startup shows `Cognee Memory Connected`.
- Confirm `COGNEE_PLUGIN_DATASET` or `~/.cognee-plugin/claude-code/config.json` matches the dataset you expect.
- If recall is empty right after a write, remember that graph building can run in the background; retry after the watcher/final sync finishes.

### Codex

Useful logs and state:

```bash
tail -f ~/.cognee-plugin/codex/hook.log
tail -f ~/.cognee-plugin/codex/subprocess.log
tail -f ~/.cognee-plugin/codex/recall-audit.log
tail -f ~/.cognee-plugin/codex/exit-watcher.log
tail -f ~/.cognee-plugin/codex/watcher.log
ls ~/.cognee-plugin/sessions/
curl -sS http://localhost:8011/health
```

Checks:

- Confirm Codex hooks are enabled before installing or testing the plugin.
- If local edits do not change behavior, reinstall the plugin from the intended marketplace/source because Codex may be running a cached copy.
- Confirm dataset and session settings before comparing recall results across terminals.

### OpenClaw

Useful checks:

```bash
openclaw cognee status
openclaw cognee health
openclaw cognee scopes
```

Checks:

- Confirm `plugins.allow` includes `cognee-openclaw` and `cognee-openclaw-skills` when using an allowlist.
- Confirm `hooks.allowConversationAccess: true` for OpenClaw versions that require it; otherwise post-turn file sync may not run.
- For cloud mode, verify which API routes your backend exposes. Some update paths are self-hosted only.

### Hermes Agent

Useful checks:

```bash
hermes cognee status
hermes cognee config
hermes cognee setup
```

Checks:

- Local-server mode is the safe default because it serializes writes to Cognee's local stores. Use embedded mode only for single-process/offline use.
- If `COGNEE_BASE_URL` is set but unreachable, the provider should fail fast rather than silently falling back to local data.
- Restart Hermes after changing provider mode, base URL, dataset, or embedded/local-server settings.

### n8n

Useful checks:

- Use the credential connection test first.
- Confirm the Base URL does not include a trailing `/api` for the standard Cognee Cloud resources; the node appends `/api` itself.
- For the Skill resource, confirm your backend exposes the `/api/v1/*` routes.
- If Search returns empty, confirm the Add Data and Cognify operations completed for the same dataset name.

## When reporting a bug

Include:

- Integration name and version or commit SHA.
- Runtime mode: local server, embedded/local SDK, remote/self-hosted, or Cognee Cloud.
- Base URL shape, with secrets removed.
- Dataset name and whether a custom session ID is configured.
- Exact symptom and the relevant log lines.
- Whether `cognify`, `improve`, or final session sync completed.
