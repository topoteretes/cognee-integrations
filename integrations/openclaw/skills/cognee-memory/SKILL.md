---
name: cognee-memory
version: 1.0.0
description: AI knowledge engine - a memory system in 6 lines of code. remember/recall/forget/improve loop, vector + graph search, OpenClaw plugin supported.
keywords: [memory,knowledge,graph,vector,search,cognee,ai,rag]
---

# Cognee Memory System

AI knowledge engine — a memory system in 6 lines of code.

> **Provenance:** contributed by ClawHub user [@smseow001](https://clawhub.ai/user/smseow001)
> (originally published as [`cognee-memory` v1.0.0](https://clawhub.ai/smseow001/cognee-memory),
> MIT-0). Translated from Chinese and adapted for this repository; the verbatim
> original is preserved in this file's git history.

**Website:** https://cognee.ai
**GitHub:** https://github.com/topoteretes/cognee
**Install:** `pip install cognee`
**OpenClaw plugin:** `@cognee/cognee-openclaw`

---

## Core API

### The four operations

| Operation | Purpose | Notes |
|-----------|---------|-------|
| `remember` | Store a memory | Persisted into the knowledge graph |
| `recall` | Query memories | Automatically routes to the best search strategy |
| `forget` | Delete memories | Remove outdated or incorrect memories |
| `improve` | Optimize learning | Continuous learning improves accuracy |

---

## Quick start

### Python API

```python
import cognee
import asyncio


async def main():
    # Store into the knowledge graph
    await cognee.remember("Cognee turns documents into AI memory.")

    # Store into the session cache (fast)
    await cognee.remember("User prefers detailed explanations.", session_id="chat_1")

    # Query (auto-routed)
    results = await cognee.recall("What does Cognee do?")
    for result in results:
        print(result)

    # Delete
    await cognee.forget(dataset="main_dataset")


asyncio.run(main())
```

### CLI

```bash
cognee-cli remember "Cognee turns documents into AI memory."
cognee-cli recall "What does Cognee do?"
cognee-cli forget --all
cognee-cli -ui  # open the local UI
```

---

## Configuration

### Environment variables

```bash
# LLM API key (required)
export LLM_API_KEY="your-openai-key"

# Or use another LLM provider
# See: https://docs.cognee.ai/setup-configuration/llm-providers

# Cognee Cloud (optional)
export COGNEE_SERVICE_URL="https://your-instance.cognee.ai"
export COGNEE_API_KEY="ck_..."
```

---

## Use cases

### 1. Customer-support agent
```
User: "My invoice issue still isn't resolved"
Cognee tracks: interaction history, failed operations, resolved cases, product history
Agent reply: "Found 2 similar billing cases resolved last month; the issue was
caused by a payment-system sync delay"
```

### 2. SQL copilot (knowledge distillation)
```
User: "How do I compute customer retention?"
Cognee tracks: expert SQL queries, workflow patterns, schema structure, successful implementations
Agent reply: "A senior analyst solved a similar retention query — here is their approach..."
```

### 3. Cross-session memory
```python
# Session 1
await cognee.remember("User prefers detailed explanations", session_id="user_123")

# Session 2 (query across sessions)
results = await cognee.recall("What does the user prefer?", session_id="user_123")
```

---

## OpenClaw plugin installation

```bash
# Install through OpenClaw's plugin manager (not plain npm):
openclaw plugins install @cognee/cognee-openclaw

# Then configure Cognee as the memory provider:
openclaw cognee setup
```

The plugin integrates automatically via OpenClaw's hook system:

- `before_prompt_build` → inject relevant memories into the prompt (auto-recall)
- `after_tool_call` → capture tool activity as trace entries
- `llm_output` → capture prompt/answer pairs into the session cache
- `agent_end` → sync changed memory files after each run
- `session_end` → bridge the session cache into the permanent knowledge graph

See the [plugin README](../../README.md) for required hook permissions
(`allowPromptInjection`, `allowConversationAccess`) and configuration options.

---

## vs. file-based memory

| Capability | File-based memory | Cognee |
|------------|-------------------|--------|
| Storage | Files | Vector + graph dual store |
| Search | Keywords | Semantic + relational |
| Learning | None | forget + improve |
| Cross-agent | Unsupported | Shared knowledge graph |
| Visualization | None | CLI UI |

---

## Deployment options

| Platform | Notes |
|----------|-------|
| Cognee Cloud | Managed service |
| Modal | Serverless, GPU autoscaling |
| Railway | Simplified PaaS |
| Fly.io | Edge deployment |
| Render | Simple PaaS |

---

## Example code

### Full memory loop

```python
import cognee
import asyncio


async def memory_loop():
    # 1. Learn new knowledge
    await cognee.remember("The user is learning Python programming")
    await cognee.remember("The user prefers learning by doing")

    # 2. Query related memories
    results = await cognee.recall("What are the user's learning preferences?")

    # 3. Improve based on feedback
    await cognee.improve("Correct the misunderstanding of the user's preferences")

    # 4. Forget incorrect memories
    await cognee.forget("The incorrect assumption")


asyncio.run(memory_loop())
```

---

## Installation status

- Python package: install `cognee`
- OpenClaw plugin: install `@cognee/cognee-openclaw` separately

---

*Powered by Cognee | https://cognee.ai*
