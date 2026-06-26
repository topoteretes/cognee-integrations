<div align="center">
  <a href="https://github.com/topoteretes/cognee">
    <img src="https://raw.githubusercontent.com/topoteretes/cognee/refs/heads/dev/assets/cognee-logo-transparent.png" alt="Cognee Logo" height="60">
  </a>

  <br />

  Cognee Integrations - AI Memory for Your Agent Framework

  <p align="center">
  <a href="https://www.youtube.com/watch?v=8hmqS2Y5RVQ&t=13s">Demo</a>
  .
  <a href="https://docs.cognee.ai/">Docs</a>
  .
  <a href="https://cognee.ai">Learn More</a>
  ·
  <a href="https://discord.gg/NQPKmU5CCg">Join Discord</a>
  ·
  <a href="https://www.reddit.com/r/AIMemory/">Join r/AIMemory</a>
  .
  <a href="https://github.com/topoteretes/cognee">Core Repo</a>
  </p>

  [![GitHub forks](https://img.shields.io/github/forks/topoteretes/cognee-integrations.svg?style=social&label=Fork&maxAge=2592000)](https://GitHub.com/topoteretes/cognee-integrations/network/)
  [![GitHub stars](https://img.shields.io/github/stars/topoteretes/cognee-integrations.svg?style=social&label=Star&maxAge=2592000)](https://GitHub.com/topoteretes/cognee-integrations/stargazers/)
  [![Downloads](https://static.pepy.tech/badge/cognee)](https://pepy.tech/project/cognee)
  [![License](https://img.shields.io/github/license/topoteretes/cognee-integrations?colorA=00C586&colorB=000000)](https://github.com/topoteretes/cognee-integrations/blob/main/LICENSE)
  [![Contributors](https://img.shields.io/github/contributors/topoteretes/cognee-integrations?colorA=00C586&colorB=000000)](https://github.com/topoteretes/cognee-integrations/graphs/contributors)
  <a href="https://github.com/sponsors/topoteretes"><img src="https://img.shields.io/badge/Sponsor-❤️-ff69b4.svg" alt="Sponsor"></a>

</div>

# Cognee Integrations

Monorepo for all Cognee-owned integration packages. Each integration gives an agent
framework (Strands, CrewAI, LangGraph, Google ADK, …) a persistent **memory layer**
backed by [cognee](https://github.com/topoteretes/cognee): a permanent knowledge graph
plus a fast session cache.

## Available Integrations

Install these from their public registries — you do **not** need to clone this monorepo to use them.

| Framework | Package | Install |
|---|---|---|
| Strands | `cognee-integration-strands` | `pip install cognee-integration-strands` |
| CrewAI | `cognee-integration-crewai` | `pip install cognee-integration-crewai` |
| LangGraph | `cognee-integration-langgraph` | `pip install cognee-integration-langgraph` |
| Google ADK | `cognee-integration-google-adk` | `pip install cognee-integration-google-adk` |
| Claude Agent SDK | `cognee-integration-claude` | `pip install cognee-integration-claude` |
| Hermes Agent | `cognee-integration-hermes-agent` | `pip install cognee-integration-hermes-agent` |
| OpenClaw | `@cognee/cognee-openclaw` | `npm install @cognee/cognee-openclaw` |
| n8n | `n8n-nodes-cognee` | install via n8n community nodes |
| Dify (Cloud) | `cognee` | install from the Dify marketplace |
| Dify (self-hosted) | `cognee-sdk` | install from the Dify marketplace |

Each integration has its own `README.md` under `integrations/<name>/` with the full tool
reference and runnable examples. The table above is generated from
[`integrations/inventory.yml`](integrations/inventory.yml) — see it for ownership,
versions, and compatible cognee ranges.

## Quickstart

The Python SDK integrations (Strands, CrewAI, LangGraph, Google ADK, Claude) share the
same three steps. Example using Strands:

**1. Install**

```bash
pip install cognee-integration-strands
pip install "strands-agents[openai]"   # the examples drive an OpenAI model
```

**2. Configure your LLM key**

Cognee extracts knowledge with an LLM, so set `LLM_API_KEY` (e.g. in a `.env` file):

```env
LLM_API_KEY=your-openai-api-key-here
```

**3. Attach the cognee tools to your agent**

```python
import os
from cognee_integration_strands import cognee_tools
from strands import Agent
from strands.models.openai import OpenAIModel

model = OpenAIModel(client_args={"api_key": os.getenv("LLM_API_KEY")}, model_id="gpt-4o")
agent = Agent(model=model, tools=cognee_tools())

# Store information in the persistent knowledge graph
agent("Remember that we signed a contract with Meditech Solutions for £1.2M.")

# A fresh agent has no chat history — it answers purely from cognee's memory
fresh = Agent(model=model, tools=cognee_tools())
print(fresh("What is the value of the Meditech Solutions contract?"))
```

### Two memory tiers

Built on cognee v1.0, every SDK integration exposes the same two tiers:

- **Permanent knowledge graph** (default) — `cognee_tools()` writes go straight to the graph.
- **Session cache** — `cognee_tools(session_id="chat_1")` routes writes to a cheap per-session
  cache (no graph extraction). Promote a session into the permanent graph later with
  `cognee.improve(session_ids=["chat_1"])`.

See the per-integration README for the exact tool names and the session-management flow.

## Structure

Each integration lives under `integrations/<name>/` and is an independently publishable package.

```
integrations/
  openclaw/           -> @openclaw/memory-cognee (npm)
  claude-code/        -> Cognee plugin for Claude Code
  codex/              -> Cognee plugin marketplace for Codex
```

## Adding a New Integration

### Python integrations

_(Template coming soon. For now, follow the TypeScript pattern below and adapt for Python with `pyproject.toml`.)_

### TypeScript/Node integrations (e.g., OpenClaw plugins)
1. Create `integrations/<name>/` with `package.json`, entry file, and plugin manifest
2. Follow the target platform's plugin conventions
3. Add an entry to `integrations/inventory.yml`

CI auto-detects new integrations by language (Python via `pyproject.toml`, TypeScript via `package.json`) — no workflow edits needed.

## Development

Each integration is developed independently with its own toolchain:

```bash
# Python integrations
cd integrations/<name>
uv sync --dev
uv run pytest tests/ -v
uv run ruff check .

# TypeScript integrations
cd integrations/<name>
npm install
npx tsc --noEmit
```

## Version Pinning Policy

Python integrations must pin the `cognee` dependency with a bounded range (e.g., `cognee>=0.5.1,<0.6.0`). This is enforced by CI via `scripts/check_version_pins.py`. TypeScript integrations that talk to Cognee via HTTP API are exempt from package pinning but should document compatible Cognee server versions.

When a new `cognee` version is released:
1. Update the bounds in affected integrations
2. Run tests to verify compatibility
3. Bump the integration version
4. Publish the updated package

## Publishing

Each integration is published independently via tag-per-package:

```bash
# TypeScript: publishes to npm
git tag openclaw-v2026.2.4 && git push --tags

# Python (when added): publishes to PyPI
# git tag <name>-v<version> && git push --tags
```

The `publish.yml` workflow parses the tag, runs tests, and publishes to the appropriate registry.

## CI

- **Lint**: Ruff on every PR across all Python integrations
- **Tests**: Auto-detects changed integrations and runs the right test suite (pytest for Python, tsc for TypeScript)
- **Pin check**: Validates bounded `cognee` dependencies in Python integrations
- **Publish**: Tag-triggered per-package publishing to PyPI or npm

## Inventory

`integrations/inventory.yml` tracks all known integrations with ownership, migration status, package names, and version info. Update it when adding or migrating integrations.
