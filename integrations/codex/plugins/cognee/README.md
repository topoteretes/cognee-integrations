# Cognee Codex Plugin

This plugin packages CLI-first Cognee workflows for Codex. It lives alongside
the Claude Code integration in `cognee-integrations`, uses the Codex plugin
manifest format, and does not configure MCP.

## Contents

- `.codex-plugin/plugin.json` - Codex plugin manifest.
- `skills/` - reusable Codex instructions for Cognee CLI workflows.
- `scripts/cognee-cli.sh` - optional helper that runs `uv run cognee-cli` from a
  Cognee repository root.

## Local Install

From the Codex marketplace root, `integrations/codex`:

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
