# Cognee-Integration-Semantic-Kernel

A [Semantic Kernel](https://github.com/microsoft/semantic-kernel) plugin that exposes [Cognee's memory layer](https://github.com/topoteretes/cognee) as kernel functions. Semantic Kernel's own memory plugins are per-process and vector-shaped; `CogneeMemoryPlugin` gives an agent a persistent knowledge graph it can write to and query across sessions.

> **Note:** This package requires Python 3.10+.

## Overview

`CogneeMemoryPlugin` is an ordinary SK plugin class with three kernel functions:

- **`remember(data)`**: store information in the knowledge graph.
- **`search(query)`**: retrieve information relevant to a query.
- **`forget()`**: clear this plugin's stored memories.

Because they're real kernel functions, they're callable like any other SK plugin function and offered to the model through auto function calling (as `cognee-remember`, `cognee-search`, `cognee-forget` when registered under `plugin_name="cognee"`).

All three call the same `cognee.remember` / `cognee.recall` / `cognee.forget` API the [strands integration](../strands) uses, so behavior stays consistent across integrations. Failures are returned as text rather than raised, so a model calling these functions sees what went wrong and can recover.

## Installation

```bash
pip install cognee-integration-semantic-kernel
```

## Quick Start

```python
import asyncio
import os

from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.function_choice_behavior import FunctionChoiceBehavior
from semantic_kernel.connectors.ai.open_ai import (
    OpenAIChatCompletion,
    OpenAIChatPromptExecutionSettings,
)
from semantic_kernel.functions import KernelArguments

from cognee_integration_semantic_kernel import CogneeMemoryPlugin

kernel = Kernel()
kernel.add_service(
    OpenAIChatCompletion(ai_model_id="gpt-4o-mini", api_key=os.getenv("LLM_API_KEY"))
)
kernel.add_plugin(CogneeMemoryPlugin(), plugin_name="cognee")

settings = OpenAIChatPromptExecutionSettings(function_choice_behavior=FunctionChoiceBehavior.Auto())
arguments = KernelArguments(settings=settings)


async def main():
    await kernel.invoke_prompt(
        "Remember that we signed a contract with Meditech Solutions for £1.2M.", arguments=arguments
    )
    print(
        await kernel.invoke_prompt(
            "What is the Meditech Solutions contract worth?", arguments=arguments
        )
    )


asyncio.run(main())
```

## Calling functions directly

The functions work without a model too, through `kernel.invoke`:

```python
await kernel.invoke(
    plugin_name="cognee", function_name="remember", data="The deploy freeze starts Friday."
)
result = await kernel.invoke(
    plugin_name="cognee", function_name="search", query="When does the deploy freeze start?"
)
print(result.value)
```

See [`examples/example.py`](examples/example.py) for a full runnable walkthrough.

## Dataset scoping

`CogneeMemoryPlugin(dataset=...)` scopes all three functions to one cognee dataset. Without one, it falls back to `COGNEE_PROJECT_NODE_SET` (the same project-scoping env var the other cognee integrations use) and then to cognee's own default (`"main_dataset"`).

```python
kernel.add_plugin(CogneeMemoryPlugin(dataset="mergetober-issues"), plugin_name="cognee")
```

> **Note:** `node_set` itself (as used by the older `add`/`cognify`/`search`-based integrations) isn't a parameter `cognee.remember`/`cognee.recall`/`cognee.forget` accept; the equivalent on this API is a dataset name. Same env var, different kwarg underneath. `forget()` always resolves to a concrete dataset (never an unscoped wipe), so a model-triggered forget can't reach past its own dataset by accident.

## Plugin Reference

### `CogneeMemoryPlugin(dataset=None, top_k=15, remember_kwargs=None, recall_kwargs=None, forget_kwargs=None)`

- `dataset`: Cognee dataset to read and write. Defaults to `COGNEE_PROJECT_NODE_SET` when set, else cognee's own default.
- `top_k`: Result count for `search`, forwarded straight to `cognee.recall`'s own `top_k`.
- `remember_kwargs` / `recall_kwargs` / `forget_kwargs`: Extra kwargs forwarded to every `cognee.remember` / `cognee.recall` / `cognee.forget` call (e.g. `session_id`).

Kernel functions:

- `remember(data: str) -> str`
- `search(query: str) -> str`: one result per line, or `"No memories found."` on an empty result.
- `forget() -> str`: takes no arguments, so a model can call it but can't redirect it at call time.

Writes (`remember`, `forget`) are serialised, since cognee isn't safe to initialise concurrently and SK's auto function calling can invoke several tool calls at once.

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

- `examples/example.py`: a kernel stores a fact through auto function calling, then a fresh kernel recalls it.

## Requirements

- Python 3.10+
- Cognee `>=1.0.0,<=1.1.2`
- Semantic Kernel `>=1.30.0,<2.0.0`
- OpenAI API key (for the example model)
