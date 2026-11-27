# Cognee-Integration-Smolagents

`cognee_remember`, `cognee_search` and `cognee_forget` — real [smolagents](https://github.com/huggingface/smolagents) `Tool` subclasses backed by [Cognee's memory layer](https://github.com/topoteretes/cognee). smolagents agents are stateless between runs; these tools give a `CodeAgent` or `ToolCallingAgent` a persistent knowledge graph across them.

> **Note:** This package requires Python 3.10+.

## Overview

Three `Tool` subclasses, usable with both `CodeAgent` and `ToolCallingAgent`:

- **`CogneeRememberTool`** (`cognee_remember`): store information in the knowledge graph.
- **`CogneeSearchTool`** (`cognee_search`): retrieve information relevant to a query.
- **`CogneeForgetTool`** (`cognee_forget`): clear this agent's stored memories.

All three call the same `cognee.remember` / `cognee.recall` / `cognee.forget` API the [strands integration](../strands) uses, so behavior stays consistent across integrations. Tool failures are caught and returned as text rather than raised, so an agent can read the error and recover instead of crashing.

## Installation

```bash
pip install cognee-integration-smolagents
```

The example drives an OpenAI model, which needs smolagents' own dependency for that:

```bash
pip install smolagents
```

## Quick Start

```python
from smolagents import CodeAgent, OpenAIModel
from cognee_integration_smolagents import cognee_tools

model = OpenAIModel(model_id="gpt-4o-mini", api_key="...")
agent = CodeAgent(tools=cognee_tools(), model=model)

agent.run("Remember that we signed a contract with Meditech Solutions for £1.2M.")

# A fresh agent, same memory:
agent2 = CodeAgent(tools=cognee_tools(), model=model)
print(agent2.run("What is the Meditech Solutions contract worth?"))
```

## Individual tools

Use one or more tools directly instead of the full set, or mix them into a larger toolbox:

```python
from cognee_integration_smolagents import CogneeRememberTool, CogneeSearchTool

agent = CodeAgent(
    tools=[CogneeRememberTool(), CogneeSearchTool(top_k=5), *other_tools],
    model=model,
)
```

See [`examples/example.py`](examples/example.py) for a full runnable walkthrough.

## Dataset scoping

All three tools accept a `dataset` argument, matching `cognee.remember`'s / `cognee.recall`'s / `cognee.forget`'s own dataset parameters. Without one, they fall back to `COGNEE_PROJECT_NODE_SET` (the same project-scoping env var the other cognee integrations use) and then to cognee's own default (`"main_dataset"`).

```python
from cognee_integration_smolagents import cognee_tools

tools = cognee_tools(dataset="repo-review-notes")
```

> **Note:** `node_set` itself (as used by the older `add`/`cognify`/`search`-based integrations) isn't a parameter `cognee.remember`/`cognee.recall`/`cognee.forget` accept; the equivalent on this API is a dataset name. Same env var, different kwarg underneath. `cognee_forget` always resolves to a concrete dataset (never an unscoped wipe), so an agent-triggered forget can't reach past its own sandbox by accident.

## Tool Reference

### `cognee_tools(dataset=None, *, top_k=15, remember_kwargs=None, recall_kwargs=None, forget_kwargs=None)`

Builds `[CogneeRememberTool, CogneeSearchTool, CogneeForgetTool]` with shared dataset scoping. Pass the result to `CodeAgent(tools=...)` / `ToolCallingAgent(tools=...)`.

### `CogneeRememberTool(dataset=None, remember_kwargs=None)`

`cognee_remember(data: str) -> str`. Stores `data`, forwarding `remember_kwargs` to every call (e.g. `session_id`).

### `CogneeSearchTool(dataset=None, top_k=15, recall_kwargs=None)`

`cognee_search(query: str) -> str`. Forwards `top_k` straight to `cognee.recall`'s own `top_k`, so cognee does the limiting. Returns `"No memories found."` on an empty result rather than an empty string.

### `CogneeForgetTool(dataset=None, forget_kwargs=None)`

`cognee_forget() -> str`. Clears the tool's configured dataset. No arguments: an agent can call it, but can't redirect it at call time.

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

- `examples/example.py`: a `CodeAgent` remembers a fact, then a fresh agent recalls it.

## Requirements

- Python 3.10+
- Cognee `>=1.0.0,<=1.1.2`
- smolagents `>=1.20.0,<2.0.0`
- OpenAI API key (for the example model)
