# cognee-openclaw-skills

OpenClaw plugin for self-improving agent skills powered by Cognee.

## How It Works

The plugin runs a feedback loop around your workspace skill definitions (`SKILL.md` files):

1. **Ingest** -- scans skill files from the workspace and sends them to Cognee for indexing.
2. **Execute** -- skills are matched and run via Cognee's skills API.
3. **Observe** -- after each agent run, records the outcome (task, skill used, success score) back to Cognee.
4. **Amendify** -- inspects failing skills, proposes fixes, and optionally applies them automatically.

## Prerequisites

- [OpenClaw](https://openclaw.dev) `>=2026.2.3-1`
- Python 3.10+ (for Cognee)
- An OpenAI API key (or another LLM provider supported by Cognee)

## Installation

Install from npm:

```bash
openclaw plugins install cognee-openclaw-skills
```

## Quick Start

1. Install and start the Cognee API server:

```bash
pip install "cognee[api]==0.5.4rc1"
export LLM_API_KEY="your-openai-api-key"
cognee-cli -api                        # starts on http://localhost:8000
curl http://localhost:8000/health      # verify it's running
```

2. Or build and install locally for development:

```bash
cd integrations/openclaw-skills
npm install
npm run build
openclaw plugins install -l .
```

3. Enable the plugin in your OpenClaw config (`~/.openclaw/openclaw.json`):

```json
{
  "cognee-openclaw-skills": {
    "enabled": true,
    "config": {
      "baseUrl": "http://localhost:8000",
      "skillsFolder": "skills",
      "requestTimeoutMs": 300000
    }
  }
}
```

The plugin authenticates with Cognee's default credentials. Set `COGNEE_API_KEY`, or `COGNEE_USERNAME` / `COGNEE_PASSWORD` env vars to override.

4. Place skills under `<workspace>/skills/<name>/SKILL.md` and start the gateway -- they will be ingested automatically on startup.

> **Note:** LLM-backed commands (`inspect`, `preview`, `auto-fix`) can take 1-3 minutes. Set `requestTimeoutMs` to at least `300000` (5 min) to avoid client-side timeouts.

## Configuration

All options live under the plugin's `config` key:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `baseUrl` | string | `http://localhost:8000` | Cognee API base URL |
| `apiKey` | string | `$COGNEE_API_KEY` | API key (alternative to username/password) |
| `username` | string | `$COGNEE_USERNAME` | Username for login |
| `password` | string | `$COGNEE_PASSWORD` | Password for login |
| `skillsFolder` | string | `skills` | Workspace-relative path to skill definitions |
| `datasetName` | string | `skills` | Cognee dataset name for skill storage |
| `autoIngest` | boolean | `true` | Ingest skills on startup |
| `autoObserve` | boolean | `true` | Record agent run outcomes |
| `autoAmendify` | boolean | `false` | Auto-fix failing skills after runs |
| `amendifyMinRuns` | number | `3` | Failed runs required before amendify triggers |
| `amendifyScoreThreshold` | number | `0.5` | Runs scoring below this count as failures |
| `requestTimeoutMs` | number | `60000` | API request timeout (ms) -- raise to 300000 for LLM calls |
| `ingestionTimeoutMs` | number | `300000` | Skill ingestion timeout (ms) |

## CLI

```
openclaw cognee-skills <command>
```

| Command | Description |
|---------|-------------|
| `ingest` | Ingest `SKILL.md` files into Cognee |
| `list` | List all ingested skills |
| `inspect <skill_id>` | Show failure analysis for a skill |
| `preview <skill_id>` | Preview a proposed amendment |
| `amendify <amendment_id>` | Apply a proposed amendment |
| `rollback <amendment_id>` | Roll back a previously applied amendment |
| `evaluate <amendment_id>` | Evaluate the effect of an amendment |
| `auto-fix <skill_id>` | Inspect, preview, and apply a fix in one step |

## Skill File Format

Each skill lives in its own directory as a `SKILL.md` file with YAML frontmatter:

```markdown
---
name: code-review
description: Review code for bugs, style issues, and suggest improvements
tags:
  - code
  - review
complexity: medium
task_patterns:
  - pattern_key: review_request
    text: "review this code"
    category: code_quality
---

## Development

```bash
npm run dev        # watch mode
npm run typecheck  # type-check without emitting
npm run clean      # remove dist/
```
