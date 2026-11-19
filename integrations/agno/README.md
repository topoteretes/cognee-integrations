# Cognee-Integration-Agno

An [Agno](https://github.com/agno-agi/agno) toolkit that gives agents [Cognee's memory layer](https://github.com/topoteretes/cognee). Agno's built-in memory is per-agent storage; `CogneeTools` gives agents a persistent knowledge graph they can write to and query across sessions.

> **Note:** This package requires Python 3.10+.

## Overview

`CogneeTools` is a native Agno `Toolkit` with three tools:

- **`cognee_remember(data)`**: store information in the knowledge graph.
- **`cognee_search(query)`**: retrieve information relevant to a query.
- **`cognee_forget()`**: clear this toolkit's stored memories.

Each tool has a sync and an async variant registered under the same name, so the toolkit works under both `agent.run()` and `agent.arun()`. All three call the same `cognee.remember` / `cognee.recall` / `cognee.forget` API the [strands integration](../strands) uses, so behavior stays consistent across integrations. Failures are returned as text rather than raised, so the agent can read what went wrong and keep going.

## Installation

```bash
pip install cognee-integration-agno
```

The example drives an OpenAI model, which needs the `openai` package:

```bash
pip install openai
```

## Quick Start

```python
import os

from agno.agent import Agent
from agno.models.openai import OpenAIChat

from cognee_integration_agno import CogneeTools

model = OpenAIChat(id="gpt-4o-mini", api_key=os.getenv("LLM_API_KEY"))

agent = Agent(model=model, tools=[CogneeTools()])
agent.print_response("Remember that we signed a contract with Meditech Solutions for £1.2M.")

# A fresh agent, same memory:
Agent(model=model, tools=[CogneeTools()]).print_response(
    "What is the Meditech Solutions contract worth?"
)
```

Async works the same way:

```python
await agent.arun("What is the Meditech Solutions contract worth?")
```

See [`examples/example.py`](examples/example.py) for a full runnable walkthrough.

## Choosing tools

Extra keyword arguments go to Agno's `Toolkit`, so its standard options apply:

```python
# read-only memory
CogneeTools(include_tools=["cognee_search"])

# ask a human before the agent wipes its memory
CogneeTools(requires_confirmation_tools=["cognee_forget"])
```

## Dataset scoping

`CogneeTools(dataset=...)` scopes all three tools to one cognee dataset. Without one, it falls back to `COGNEE_PROJECT_NODE_SET` (the same project-scoping env var the other cognee integrations use) and then to cognee's own default (`"main_dataset"`).

```python
CogneeTools(dataset="team-decisions")
```

> **Note:** `node_set` itself (as used by the older `add`/`cognify`/`search`-based integrations) isn't a parameter `cognee.remember`/`cognee.recall`/`cognee.forget` accept; the equivalent on this API is a dataset name. Same env var, different kwarg underneath. `cognee_forget` always resolves to a concrete dataset (never an unscoped wipe), so an agent-triggered forget can't reach past its own dataset by accident.

## Toolkit Reference

### `CogneeTools(dataset=None, top_k=15, remember_kwargs=None, recall_kwargs=None, forget_kwargs=None, **kwargs)`

- `dataset`: Cognee dataset to read and write. Defaults to `COGNEE_PROJECT_NODE_SET` when set, else cognee's own default.
- `top_k`: Result count for `cognee_search`, forwarded straight to `cognee.recall`'s own `top_k`.
- `remember_kwargs` / `recall_kwargs` / `forget_kwargs`: Extra kwargs forwarded to every `cognee.remember` / `cognee.recall` / `cognee.forget` call (e.g. `session_id`).
- `**kwargs`: Forwarded to Agno's `Toolkit` (`include_tools`, `exclude_tools`, `requires_confirmation_tools`, ...).

Tools:

- `cognee_remember(data: str) -> str`
- `cognee_search(query: str) -> str`: one result per line, or `"No memories found."` on an empty result.
- `cognee_forget() -> str`: takes no arguments, so an agent can call it but can't redirect it at call time.

Writes (`cognee_remember`, `cognee_forget`) are serialised, since cognee isn't safe to initialise concurrently and an agent can fire several tool calls at once.

## Configuration

Copy `.env.template` to `.env` and set your key:

```bash
cp .env.template .env
```

```env
LLM_API_KEY=your-openai-api-key-here
COGNEE_PROJECT_NODE_SET=
```

## Examples

- `examples/example.py`: an agent stores a fact, then a fresh agent recalls it.

## Requirements

- Python 3.10+
- Cognee `>=1.0.0,<=1.1.2`
- Agno `>=3.0.0,<4.0.0`
- OpenAI API key (for the example model)
