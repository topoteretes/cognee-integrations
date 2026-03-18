# OpenClaw Skills Plugin Examples

## Prerequisites

- [OpenClaw](https://openclaw.dev) CLI installed (`>=2026.2.3-1`).
- Python 3.10+ with Cognee installed, **or** a running Cognee API server.
- An OpenAI API key (or another LLM provider supported by Cognee).

## Example: Self-Improving Skills Setup

This walkthrough installs the plugin, creates a skill, ingests it, and runs the feedback loop.

### 1. Start the Cognee API server

```bash
pip install "cognee[api]==0.5.4rc1"
export LLM_API_KEY="your-openai-api-key"
cognee-cli -api
# Verify: curl http://localhost:8000/health
```

### 2. Install the plugin

```bash
# From the monorepo (development)
cd integrations/openclaw-skills
npm install && npm run build
openclaw plugins install -l .

# Or from npm (once published)
openclaw plugins install cognee-openclaw-skills
```

### 3. Configure the plugin

Add to `~/.openclaw/openclaw.json`:

```json
{
  "cognee-openclaw-skills": {
    "enabled": true,
    "config": {
      "baseUrl": "http://localhost:8000",
      "skillsFolder": "skills",
      "autoIngest": true,
      "autoObserve": true,
      "requestTimeoutMs": 300000
    }
  }
}
```

### 4. Create a skill definition

```bash
mkdir -p skills/code-review
cat > skills/code-review/SKILL.md << 'SKILLEOF'
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

You are a thorough code reviewer. When given code, analyze it for:
1. Bugs and logical errors
2. Style and readability issues
3. Performance concerns
4. Security vulnerabilities

Provide specific, actionable suggestions with code examples.
SKILLEOF
```

### 5. Ingest skills and verify

```bash
# Ingest skill files into Cognee
openclaw cognee-skills ingest

# List ingested skills
openclaw cognee-skills list
```

### 6. Run the feedback loop

Start OpenClaw and ask it to review some code. The plugin will:
1. Match the task to the `code-review` skill.
2. Execute using the skill instructions.
3. Observe the outcome (success score).

After multiple runs, inspect how the skill is performing:

```bash
# Analyze failures for a skill
openclaw cognee-skills inspect <skill_id>

# Preview a proposed fix
openclaw cognee-skills preview <skill_id>

# Apply the fix
openclaw cognee-skills amendify <amendment_id>

# Or do it all at once
openclaw cognee-skills auto-fix <skill_id>
```

## Async-first Notes

Inspection and amendment workflows may run for minutes due to LLM calls. Use higher request timeout values and avoid blocking synchronous request handlers while amendify/evaluation tasks are executing.

