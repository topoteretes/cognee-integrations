# Cognee-Integration-Strands

A powerful integration between Cognee and Strands that provides intelligent knowledge management and retrieval capabilities for AI agents.

> **Note:** This package requires Python 3.10+.

## Overview

`cognee-integration-strands` combines [Cognee's memory layer](https://github.com/topoteretes/cognee) with the [Strands Agents](https://github.com/strands-agents/harness-sdk) framework. Build agents that store, search, and recall information from a persistent knowledge graph — plus a fast session cache.

## Features

- **Smart Knowledge Storage**: Persist information into Cognee's knowledge graph.
- **Semantic Search**: Retrieve relevant information with natural-language queries.
- **Two memory tiers**: a permanent knowledge graph plus a fast session cache you persist with `improve()`.
- **Strands Integration**: Drop-in tools for the Strands `Agent`.
- **Background Async Support**: Cognee's async API is driven on a background thread, so the synchronous Strands tools just work.

## Upgrading from 0.1.x ⚠️

`0.2.0` moves the integration to **cognee v1.0** and replaces the old tool API. It's a breaking change with no compatibility shim — update your imports:

| 0.1.x | 0.2.0 |
|---|---|
| `from cognee_integration_strands import add_tool, search_tool` | `from cognee_integration_strands import cognee_tools` |
| `add_tool, search_tool = get_sessionized_cognee_tools("user-1")` | `tools = cognee_tools(session_id="user-1")` |
| `Agent(tools=[add_tool, search_tool])` | `Agent(tools=cognee_tools())` |
| `cognee>=0.4.0,<0.5.4` | `cognee>=1.0.0,<=1.1.2` |
| `strands-agents>=1.18.0` | `strands-agents>=1.42.0` |

In 0.1.x a `session_id` tagged data to isolate it per user. In 0.2.0 it routes writes to cognee's **session cache**; run `cognee.improve(session_ids=[session_id])` to persist a session into the permanent graph (see [Session Management](#session-management)).

## Installation

```bash
pip install cognee-integration-strands
```

The examples drive an OpenAI model, which needs the Strands `openai` extra:

```bash
pip install "strands-agents[openai]"
```

## Quick Start

```python
import os
import cognee
from cognee_integration_strands import cognee_tools, run_cognee_task
from strands import Agent
from strands.models.openai import OpenAIModel

run_cognee_task(cognee.forget(everything=True))  # optional: start fresh

model = OpenAIModel(client_args={"api_key": os.getenv("LLM_API_KEY")}, model_id="gpt-4o")
agent = Agent(model=model, tools=cognee_tools())

# Store information
agent("Remember that we signed a contract with Meditech Solutions for £1.2M.")

# Retrieve it (even from a fresh agent — memory is persistent)
print(agent("What is the value of the Meditech Solutions contract?"))
```

## Available Tools

```python
from cognee_integration_strands import cognee_tools

# cognee_tools() -> [remember, recall]
#   remember: store information   (cognee.remember)
#   recall:   retrieve information (cognee.recall)
```

Pass `cognee_tools(session_id=...)` to route writes through cognee's session cache.

## Session Management

A `session_id` selects cognee's **session cache** tier instead of the permanent graph:

- **No `session_id`** → `remember` writes straight to the permanent knowledge graph.
- **With `session_id`** → `remember` writes to that session's cache (cheap, no graph extraction); recall is session-aware.
- **`cognee.improve(session_ids=[session_id])`** → promotes a session's cached entries into the permanent graph.

So an agent can capture context cheaply during a session, then persist the useful parts later. Pass `remember_kwargs={"self_improvement": False}` to keep cached writes out of the graph until you call `improve()` (otherwise cognee bridges them in the background).

```python
import cognee
from cognee_integration_strands import cognee_tools, run_cognee_task
from strands import Agent

SESSION_ID = "mission-briefing"

session_agent = Agent(
    model=model,
    tools=cognee_tools(session_id=SESSION_ID, remember_kwargs={"self_improvement": False}),
)
# ... use session_agent to remember/recall during the session ...

# Persist everything captured in the session into the permanent graph:
run_cognee_task(cognee.improve(session_ids=[SESSION_ID]))
```

For a full runnable walkthrough, see [`examples/session_example.py`](examples/session_example.py).

## Tool Reference

### `cognee_tools(session_id=None, *, remember_kwargs=None, recall_kwargs=None)`

Builds the `remember` and `recall` Strands tools. Pass the result to `Agent(tools=...)`. With `session_id`, writes go to cognee's session cache (persist later with `cognee.improve(session_ids=[session_id])`); without it, writes go straight to the permanent graph. `remember_kwargs` / `recall_kwargs` bind extra cognee params per call (e.g. `remember_kwargs={"self_improvement": False}`).

**Returns:** `[remember, recall]`

### `remember(data, **kwargs)` / `recall(query_text, **kwargs)`

Synchronous passthroughs to `cognee.remember` / `cognee.recall` for direct use outside an agent (they run cognee on a background loop). No defaults are imposed — pass any cognee parameter. `recall` returns cognee's native `RecallResponse` list; flatten it with `render_results(...)`.

### `run_cognee_task(coro, timeout=300)`

Runs any async cognee coroutine (e.g. `cognee.improve(...)`, `cognee.forget(...)`) from synchronous code and returns the result.

## Configuration

Copy `.env.template` to `.env` and set your key:

```bash
cp .env.template .env
```

```env
LLM_API_KEY=your-openai-api-key-here
```

## Examples

- `examples/example.py`: store contracts with an agent, then recall them from a fresh agent.
- `examples/session_example.py`: the session cache → `improve()` → permanent graph flow, with before/after graph visualizations.

## Requirements

- Python 3.10+
- Cognee `>=1.0.0,<=1.1.2`
- Strands Agents `>=1.42.0` (`strands-agents[openai]` for the examples)
- OpenAI API key (for the example model)
