# Cognee-Integration-Claude

A powerful integration between Cognee and Claude Agent SDK that provides intelligent memory management and retrieval capabilities for AI agents.

## Overview

`cognee-integration-claude` combines [Cognee's advanced memory layer](https://github.com/topoteretes/cognee) with Anthropic's [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python). This integration allows you to build AI agents that can efficiently store, search, and retrieve information from a persistent knowledge base.

## Features

- **Smart Knowledge Storage**: Add and persist information using Cognee's advanced indexing
- **Semantic Search**: Retrieve relevant information using natural language queries
- **Two memory tiers**: a permanent knowledge graph plus a fast session cache you persist with `improve()`
- **Claude Agent SDK Integration**: Seamless integration with Claude's agent framework
- **Async Support**: Built with async/await for high-performance applications
- **Cross-Session Persistence**: Memory survives between agent instances

## Upgrading from 0.1.x ⚠️

cognee-integration-claude `0.2.0` moves the integration to **cognee v1.0** and replaces the old tool API.
It's a breaking change with no compatibility shim — update your imports:

| 0.1.x | 0.2.0 |
|---|---|
| `from cognee_integration_claude import add_tool, search_tool` | `from cognee_integration_claude import cognee_tools` |
| `tools=[add_tool, search_tool]` | `tools=cognee_tools()` |
| `get_sessionized_cognee_tools("user-1")` | `cognee_tools(session_id="user-1")` |
| `allowed_tools=["mcp__tools__add_tool", "mcp__tools__search_tool"]` | `allowed_tools=["mcp__tools__remember", "mcp__tools__recall"]` |
| `cognee>=0.3.4,<0.5.4` | `cognee>=1.0.0,<=1.1.2` |

In 0.1.x a `session_id` tagged data to isolate it per user. In 0.2.0 it routes writes to cognee's **session cache**; run `cognee.improve(session_ids=[session_id])` to persist a session into the permanent graph (see [Session Management](#session-management)).

## Installation

```bash
pip install cognee-integration-claude
```

Or using uv:

```bash
uv add cognee-integration-claude
```

## Quick Start

```python
import asyncio
import os
from dotenv import load_dotenv
import cognee
from claude_agent_sdk import (
    create_sdk_mcp_server,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    AssistantMessage,
    TextBlock,
)
from cognee_integration_claude import cognee_tools

load_dotenv()

async def main():
    # Clean up memory to start fresh (Optional)
    await cognee.forget(everything=True)
    
    # Create an MCP server with Cognee tools
    server = create_sdk_mcp_server(
        name="cognee-tools",
        version="1.0.0",
        tools=cognee_tools()
    )
    
    # Configure the agent
    options = ClaudeAgentOptions(
        mcp_servers={"tools": server},
        allowed_tools=["mcp__tools__remember", "mcp__tools__recall"],
    )
    
    # Use the agent to store information
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Remember that our company signed a contract with HealthBridge Systems "
            "in the healthcare industry, starting Feb 2023, ending Jan 2026, worth £2.4M"
        )
        
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"Claude: {block.text}")
    
    # Query the stored information (new agent instance)
    async with ClaudeSDKClient(options=options) as client:
        await client.query("What contracts do we have in the healthcare industry?")
        
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"Claude: {block.text}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Available Tools

### Basic Tools

```python
from cognee_integration_claude import cognee_tools

# cognee_tools() -> [remember_tool, recall_tool]
#   remember (mcp__<server>__remember): store information   (cognee.remember)
#   recall   (mcp__<server>__recall):   retrieve information (cognee.recall)
```

### Session Tools

Pass a `session_id` to route writes to cognee's **session cache** instead of the
permanent graph. Cached entries are persisted into the graph when you call
`cognee.improve(session_ids=[session_id])` — see [Session Management](#session-management).

```python
from cognee_integration_claude import cognee_tools

# Writes go to the session cache (until improve())
tools = cognee_tools(session_id="mission-briefing")

# No session -> writes go straight to the permanent graph
tools = cognee_tools()
```

## Session Management

A `session_id` selects cognee's **session cache** tier instead of the permanent graph:

- **No `session_id`** → `remember` writes straight to the permanent knowledge graph.
- **With `session_id`** → `remember` writes to that session's cache (cheap, no graph extraction); recall is session-aware.
- **`cognee.improve(session_ids=[session_id])`** → promotes a session's cached entries into the permanent graph.

So an agent can capture context cheaply during a session, then persist the useful parts later. Pass `remember_kwargs={"self_improvement": False}` to keep cached writes out of the graph until you call `improve()` (otherwise cognee bridges them in the background).

```python
import cognee
from cognee_integration_claude import cognee_tools

SESSION_ID = "mission-briefing"

# Session agent: writes land in the cache, not the graph (until improve()).
session_tools = cognee_tools(
    session_id=SESSION_ID,
    remember_kwargs={"self_improvement": False},
)
# ... drive an agent with session_tools to remember/recall during the session ...

# Persist everything captured in the session into the permanent graph:
await cognee.improve(session_ids=[SESSION_ID])
```

For a full runnable walkthrough — a permanent agent that can't see a session's cached data until `improve()` bridges it — see [`examples/session_memory.py`](examples/session_memory.py).

## Tool Reference

### `cognee_tools(session_id=None, *, remember_kwargs=None, recall_kwargs=None)`

Builds the `remember` and `recall` MCP tools. Pass the result to
`create_sdk_mcp_server`; the agent calls them as `mcp__<server>__remember` and
`mcp__<server>__recall`. With `session_id`, writes go to cognee's session cache
(persist later with `cognee.improve(session_ids=[session_id])`); without it,
writes go straight to the permanent graph. `remember_kwargs` / `recall_kwargs`
bind extra cognee params per call (e.g. `remember_kwargs={"self_improvement": False}`).

**Returns:** `[remember_tool, recall_tool]`

```python
from cognee_integration_claude import cognee_tools

server = create_sdk_mcp_server(
    name="memory-tools",
    version="1.0.0",
    tools=cognee_tools(),                  # or cognee_tools(session_id="user-123")
)

options = ClaudeAgentOptions(
    mcp_servers={"tools": server},
    allowed_tools=["mcp__tools__remember", "mcp__tools__recall"],
)

async with ClaudeSDKClient(options=options) as client:
    await client.query("Store this: Our Q4 revenue was $2.5M with 15% growth")
    async for msg in client.receive_response():
        pass
```

### `remember(data, **kwargs)`

Thin passthrough to `cognee.remember`, for pre-loading data directly (outside an
agent). The integration imposes **no defaults of its own** — pass any keyword
argument `cognee.remember` accepts (`dataset_name`, `session_id`,
`self_improvement`, `run_in_background`, `custom_prompt`, `node_set`,
`importance_weight`, …), and anything you omit falls back to cognee's own
defaults. Returns cognee's `RememberResult`.

```python
from cognee_integration_claude import remember

await remember("Einstein was born in Ulm.")                       # cognee defaults
```

### `recall(query_text, **kwargs)`

Thin passthrough to `cognee.recall`. Again **no integration defaults** — pass any
keyword argument `cognee.recall` accepts (`query_type`, `datasets`, `top_k`,
`session_id`, `node_name`, `scope`, `user`, …). With no `query_type`, cognee
auto-routes the search strategy. Returns cognee's native `RecallResponse` list;
use `render_results(...)` to flatten it to plain strings.

```python
import cognee
from cognee_integration_claude import recall, render_results

results = await recall("healthcare contracts", query_type=cognee.SearchType.GRAPH_COMPLETION, top_k=20)
texts = render_results(results)
```

### `render_results(results)`

Flattens cognee's native `RecallResponse` list (what `recall` returns) into a
list of plain strings, picking the right text field per result source.

```python
from cognee_integration_claude import recall, render_results

texts = render_results(await recall("healthcare contracts"))
```

## Configuration

### Environment Variables

Create a `.env` file in your project root:

```bash
# OpenAI API key (used by Cognee for LLM operations)
LLM_API_KEY=your-openai-api-key-here
```

You can also use other LLM providers with Cognee. Check the [Cognee documentation](https://docs.cognee.ai/setup-configuration/llm-providers) for more details.

The Claude Agent SDK uses a bundled Claude Code CLI that handles authentication automatically. 

1. **If you're using Cursor**: You're likely already authenticated through your Cursor/Claude session. The bundled CLI will use your existing credentials.

2. **If you're running standalone**: The first time you run the SDK, it will guide you through authentication via OAuth or API key through the bundled CLI.

3. **For CI/CD or automated environments**: You may need to authenticate the CLI separately. See the [Claude Agent SDK documentation](https://github.com/anthropics/claude-agent-sdk-python) for details.

### Cognee Configuration (Optional)

You can customize Cognee's data and system directories:

```python
from cognee.api.v1.config import config
import os

config.data_root_directory(
    os.path.join(os.path.dirname(__file__), ".cognee/data_storage")
)

config.system_root_directory(
    os.path.join(os.path.dirname(__file__), ".cognee/system")
)
```

## Examples

Check out the `examples/` directory for simple examples:

- **`examples/example.py`**: Basic usage with add and search tools

And the interactive guide:

- **`cognee_integration_claude/guide.ipynb`**: Step-by-step Jupyter notebook tutorial

## Advanced Usage

### Pre-loading Data

You can pre-load data into Cognee before creating agents:

```python
import asyncio
import cognee
from claude_agent_sdk import (
    create_sdk_mcp_server,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    AssistantMessage,
    TextBlock,
)
from cognee_integration_claude import cognee_tools

async def main():
    # Pre-load data directly into Cognee. cognee.remember extracts entities and
    # relationships and persists them — no separate cognify() step needed.
    await cognee.remember("Important company information here...")
    await cognee.remember("More data to remember...")
    
    # Now create an agent that can search this data
    server = create_sdk_mcp_server(
        name="cognee-tools",
        version="1.0.0",
        tools=cognee_tools()
    )
    
    # Allow only recall if you want a read-only agent
    options = ClaudeAgentOptions(
        mcp_servers={"tools": server},
        allowed_tools=["mcp__tools__recall"],
    )
    
    async with ClaudeSDKClient(options=options) as client:
        await client.query("What information do we have?")
        
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(block.text)

if __name__ == "__main__":
    asyncio.run(main())
```

### Data Management

```python
import asyncio
import cognee

async def reset_knowledge_base():
    """Clear all data and reset the knowledge base"""
    await cognee.forget(everything=True)

async def visualize_knowledge_graph():
    """Render the knowledge graph.

    Plain cognee.visualize_graph() can't see per-dataset graphs when cognee's
    access control is enabled (the default), so name the datasets explicitly.
    """
    from cognee.api.v1.visualize import visualize_multi_user_graph
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    pairs = [(user, ds) for ds in await cognee.datasets.list_datasets(user=user)]
    await visualize_multi_user_graph(pairs, destination_file_path="graph.html")
```

### Disabling Default Cursor Tools

When using Claude Agent SDK in environments like Cursor, you may want to disable default tools:

```python
options = ClaudeAgentOptions(
    mcp_servers={"tools": server},
    allowed_tools=["mcp__tools__remember", "mcp__tools__recall"],
    disallowed_tools=[
        "Task", "Bash", "Glob", "Grep", "ExitPlanMode",
        "Read", "Edit", "Write", "NotebookEdit", "WebFetch",
        "TodoWrite", "WebSearch", "BashOutput", "KillShell", "SlashCommand",
    ],
)
```

## Requirements

- Python 3.13+
- OpenAI API key (or other LLM provider supported by Cognee)

## Related Projects

- [cognee](https://github.com/topoteretes/cognee) - The core Cognee memory layer
- [claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python) - Claude Agent SDK

## License

MIT License

## Contributing

Contributions are welcome! Please feel free fork to submit a PR.

