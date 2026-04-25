# Cognee Plugin Marketplace for Codex

This integration packages Cognee skills for Codex as a local Codex plugin
marketplace. It exposes CLI-first workflows for Cognee setup, memory, codebase
ingestion, and local UI launch.

## Contents

- `.agents/plugins/marketplace.json` - local Codex marketplace definition.
- `plugins/cognee/.codex-plugin/plugin.json` - Cognee Codex plugin manifest.
- `plugins/cognee/skills/` - reusable Codex skills for Cognee CLI workflows.
- `plugins/cognee/scripts/cognee-cli.sh` - helper that runs `uv run cognee-cli`
  from a Cognee repository root.

## Local Install

From this directory:

```bash
codex plugin marketplace add .
```

Restart Codex, open the plugin directory, select `Cognee Local Plugins`, and
install `Cognee`.

## CLI Baseline

The skills assume Cognee is available through the repository environment:

```bash
uv run cognee-cli --help
uv run cognee-cli remember "Cognee turns documents into AI memory." -d notes
uv run cognee-cli recall "What does Cognee do?" -d notes
uv run cognee-cli -ui
```
