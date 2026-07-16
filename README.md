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

The Claude Code integration is a **plugin** — it gives Claude Code persistent memory
across sessions with no code to write. It auto-captures your prompts, tool traces, and
responses, and auto-recalls relevant context on every prompt.

**1. Install the plugin**

Run these slash commands directly in the Claude Code chat:

```
/plugin marketplace add topoteretes/cognee-integrations
/plugin install cognee-memory@cognee
```

**2. Configure your LLM key**

In local mode (the default), the plugin bootstraps a local Cognee API on
`http://localhost:8011`. Cognee extracts knowledge with an LLM, so set `LLM_API_KEY`
in the shell that launches Claude Code:

```bash
export LLM_API_KEY="sk-..."
```

To target Cognee Cloud or a remote server instead, set `COGNEE_BASE_URL` and
`COGNEE_API_KEY`. On startup you should see a **"Cognee Memory Connected"** message.

**3. Use Claude Code as usual**

Memory is captured and recalled automatically — no extra steps. You can also invoke the
skills explicitly:

```
/cognee-memory:cognee-remember   # store something now
/cognee-memory:cognee-search     # query memory
/cognee-memory:cognee-sync       # persist the session into the graph
```

For full configuration (datasets, sessions, sync watchers, cloud mode), see
[`integrations/claude-code/README.md`](integrations/claude-code/README.md).

> **Using an agent framework instead?** The Python SDK integrations (Strands, CrewAI,
> LangGraph, Google ADK, Claude Agent SDK) follow a `pip install` →
> set `LLM_API_KEY` → attach `cognee_tools()` pattern. See each integration's README
> under `integrations/<name>/` for a runnable example.

### Two memory tiers

Built on cognee v1.0, the integrations share the same two tiers:

- **Permanent knowledge graph** — durable memory that survives across sessions.
- **Session cache** — a cheap per-session cache (no graph extraction up front) that is
  promoted into the permanent graph on sync (`/cognee-memory:cognee-sync`, or
  `cognee.improve(session_ids=[...])` in the SDK integrations).

## Using the Python Integrations

Every Python integration installs from PyPI and follows the same shape: **install →
set `LLM_API_KEY` → build the cognee tools → pass them to your agent.** The only thing
that differs per framework is the import line and how you construct the agent.

```bash
pip install cognee-integration-strands       # or -crewai, -langgraph, -google-adk, -claude
export LLM_API_KEY="sk-..."                   # cognee extracts knowledge with an LLM
```

The tools come in two styles depending on the integration's version:

| Framework | Package | Build the tools with | Tools |
|---|---|---|---|
| Strands | `cognee-integration-strands` | `cognee_tools(session_id=None)` | `remember`, `recall` |
| Claude Agent SDK | `cognee-integration-claude` | `cognee_tools(session_id=None)` | `remember`, `recall` |
| CrewAI | `cognee-integration-crewai` | `from … import add_tool, search_tool` | `add_tool`, `search_tool` |
| Google ADK | `cognee-integration-google-adk` | `from … import add_tool, search_tool` | `add_tool`, `search_tool` |
| LangGraph | `cognee-integration-langgraph` | `get_sessionized_cognee_tools(user_id)` | `add_tool`, `search_tool` |

**`cognee_tools()` style** (cognee v1.0 — Strands, Claude Agent SDK). Writes go to the
permanent graph; pass `session_id=...` to use the session cache instead:

```python
from cognee_integration_strands import cognee_tools
from strands import Agent
from strands.models.openai import OpenAIModel

agent = Agent(model=OpenAIModel(...), tools=cognee_tools())
agent("Remember that we signed a contract with Meditech Solutions for £1.2M.")
print(agent("What is the value of the Meditech Solutions contract?"))
```

**`add_tool` / `search_tool` style** (CrewAI, Google ADK, LangGraph). Here you also ingest
source documents yourself with `cognee.add(...)` + `cognee.cognify()` before searching:

```python
import cognee
from cognee_integration_crewai import add_tool, search_tool   # CrewAI / Google ADK
from crewai import Agent

await cognee.add("Meditech Solutions — healthcare industry, contract worth £1.2M.")
await cognee.cognify()                                        # build the knowledge graph

agent = Agent(role="Analyst", goal="…", backstory="…", tools=[add_tool, search_tool])
print(agent.kickoff("Which contracts are in the healthcare industry?"))
```

LangGraph is the same style but builds its tools per user:
`add_tool, search_tool = get_sessionized_cognee_tools("user-1")`.

Each integration's `README.md` under `integrations/<name>/` has a complete runnable
example (`examples/`) and the full tool reference.

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
