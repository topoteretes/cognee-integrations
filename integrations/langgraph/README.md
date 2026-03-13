# Cognee-Integration-LangGraph

A powerful integration between Cognee and LangGraph that provides intelligent knowledge management and retrieval capabilities for AI agents.

> **Note:** This package requires Python 3.10+ and uses async tools. All agents must use `await agent.ainvoke()` instead of `agent.invoke()`

## Overview

`cognee-integration-langgraph` combines Cognee's advanced knowledge storage and retrieval system with LangGraph's workflow orchestration capabilities. This integration allows you to build AI agents that can efficiently store, search, and retrieve information from a persistent knowledge base.

## Features

- **Smart Knowledge Storage**: Add and persist information using Cognee's advanced indexing
- **Semantic Search**: Retrieve relevant information using natural language queries
- **Session Management**: Support for user-specific data isolation
- **LangGraph Integration**: Seamless integration with LangGraph's agent framework
- **Async Support**: Built with async/await for high-performance applications

## Installation

```bash
# Basic installation
pip install cognee-integration-langgraph

# With guide dependencies (needed for examples/guide.ipynb)
pip install cognee-integration-langgraph[guide]
```

The `[guide]` extra includes additional dependencies (`mediawikiapi`, `wikibase-rest-api-client`) needed for the WikiData functionality demonstrated in the guide notebook.

## Quick Start

```python
import asyncio
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from cognee_integration_langgraph import get_sessionized_cognee_tools
import cognee

async def main():  
    # Get sessionized tools with a custom session ID
    add_tool, search_tool = get_sessionized_cognee_tools("user-123")
    
    # Or get regular tools without sessionization (auto-generates a session ID)
    # add_tool, search_tool = get_sessionized_cognee_tools()
    
    # Create an agent with memory capabilities
    agent = create_agent(
        "openai:gpt-4o-mini",
        tools=[add_tool, search_tool],
    )
    
    # Use the agent (note: must use await with .ainvoke())
    response = await agent.ainvoke({
        "messages": [
            HumanMessage(content="Remember: I like pizza and coding in Python")
        ]
    })
    
    print(response["messages"][-1].content)

if __name__ == "__main__":
    asyncio.run(main())
```

## Available Tools

### `get_sessionized_cognee_tools(session_id: Optional[str] = None, include_persist_tool: bool = False, user=None)`
Returns cognee tools with optional user-specific sessionization.

**Parameters:**
- `session_id` (optional): User identifier for data isolation. If not provided, a random session ID is auto-generated.
- `include_persist_tool` (optional): If True, include `persist_sessions_tool` in the returned list. Default is False.
- `user` (optional): Cognee User object for access control (required when `ENABLE_BACKEND_ACCESS_CONTROL=True`).

**Returns:** `(add_tool, search_tool)` or `(add_tool, search_tool, persist_sessions_tool)` - Tools for storing and searching data

**Usage:**
```python
# With sessionization (recommended for multi-user apps)
add_tool, search_tool = get_sessionized_cognee_tools("user-123")

# Without explicit session (auto-generates session ID)
add_tool, search_tool = get_sessionized_cognee_tools()
```

### Individual Tools
- **`add_tool`**: Store information in the knowledge base
- **`search_tool`**: Search and retrieve previously stored information

## Session Management

`cognee-integration-langgraph` supports user-specific sessions to isolate data between different users or contexts:

```python
import asyncio
from cognee_integration_langgraph import get_sessionized_cognee_tools
from langchain.agents import create_agent

async def main():
    # Each user gets their own isolated session
    user1_add, user1_search = get_sessionized_cognee_tools("user-123")
    user2_add, user2_search = get_sessionized_cognee_tools("user-456")
    
    # Create separate agents for each user
    agent1 = create_agent("openai:gpt-4o-mini", tools=[user1_add, user1_search])
    agent2 = create_agent("openai:gpt-4o-mini", tools=[user2_add, user2_search])
    
    # Each agent works with isolated data
    await agent1.ainvoke({"messages": [...]})
    await agent2.ainvoke({"messages": [...]})
```

## Configuration

Copy the `.env.template` file to `.env` and fill out the required API keys:

```bash
cp .env.template .env
```

Then edit the `.env` file and set both keys using your OpenAI API key:

```env
OPENAI_API_KEY=your-openai-api-key-here
LLM_API_KEY=your-openai-api-key-here
```

## Examples

Check out the `examples/` directory for more comprehensive usage examples:

- `examples/example.py`: Complete workflow with contract management
- `examples/guide.ipynb`: Jupyter notebook tutorial with step-by-step guidance
- `examples/saas_entitlements_agents.py`: Multi-agent SaaS entitlements demo (see `examples/README_SAAS_DEMO.md`)
- `examples/memory_reuse_experiment.py`: Compares baseline graph search vs. Redis session cache + memify across repeated incident investigations. Per-run Q&A history accumulates in Redis; every `MEMIFY_EVERY` incidents it is persisted to the knowledge graph and flushed. Outputs a timing table, cache depth chart, and an interactive graph visualization.

**Redis setup (required for the experiment)**

```bash
# macOS
brew install redis && brew services start redis

# Ubuntu/Debian
sudo apt-get install redis-server && sudo systemctl start redis

redis-cli ping  # should return PONG
```

Add to `.env`:

```env
CACHING=true
CACHE_BACKEND=redis
```

## Requirements

- Python 3.10+
- OpenAI API key
- Dependencies automatically managed via pyproject.toml