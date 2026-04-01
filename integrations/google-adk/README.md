# Cognee-Integration-Google-ADK

A powerful integration between Cognee and Google ADK that provides intelligent memory management and retrieval capabilities for AI agents.

## Overview

`cognee-integration-google-adk` combines [Cognee's advanced memory layer](https://github.com/topoteretes/cognee) with Google's Agent Development Kit (ADK). This integration allows you to build AI agents that can efficiently store, search, and retrieve information from a persistent knowledge base.

## Features

- **Smart Knowledge Storage**: Add and persist information using Cognee's advanced indexing
- **Semantic Search**: Retrieve relevant information using natural language queries
- **Session Management**: Support for user-specific data isolation
- **Google ADK Integration**: Seamless integration with Google's Agent Development Kit
- **Async Support**: Built with async/await for high-performance applications
- **Long-Running Tools**: Optimized for Google ADK's long-running tool capabilities
- **Thread-Safe**: Queue-based processing for concurrent operations

## Installation

```bash
pip install cognee-integration-google-adk
```

## Quick Start

```python
import asyncio
from dotenv import load_dotenv
import cognee
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from cognee_integration_google_adk import add_tool, search_tool

load_dotenv()

async def main():
    # Initialize Cognee (optional - for data management)
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    
    # Create an agent with memory capabilities
    agent = Agent(
        model="gemini-2.5-flash",
        name="research_analyst",
        description="You are an expert research analyst with access to a comprehensive knowledge base.",
        instruction="You are an expert research analyst with access to a comprehensive knowledge base.",
        tools=[add_tool, search_tool],
    )
    
    runner = InMemoryRunner(agent=agent)
    
    # Use the agent to store information
    events = await runner.run_debug(
        "Remember that our company signed a contract with HealthBridge Systems "
        "in the healthcare industry, starting Feb 2023, ending Jan 2026, worth £2.4M"
    )
    
    # Print agent response
    for event in events:
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if part.text:
                    print(part.text)
    
    # Query the stored information
    events = await runner.run_debug(
        "What contracts do we have in the healthcare industry?"
    )
    
    for event in events:
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if part.text:
                    print(part.text)

if __name__ == "__main__":
    asyncio.run(main())
```

## Available Tools

### Basic Tools

```python
from cognee_integration_google_adk import add_tool, search_tool

# add_tool: Store information in the knowledge base
# search_tool: Search and retrieve previously stored information
```

### Sessionized Tools

For multi-user applications, use sessionized tools to isolate data between users:

```python
from cognee_integration_google_adk import get_sessionized_cognee_tools

# Get tools for a specific user session
add_tool, search_tool = get_sessionized_cognee_tools("user-123")

# Auto-generate a session ID
add_tool, search_tool = get_sessionized_cognee_tools()
```

## Session Management

`cognee-integration-google-adk` supports user-specific sessions to tag data and isolate retrieval between different users or contexts:

```python
import asyncio
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from cognee_integration_google_adk import get_sessionized_cognee_tools

async def main():
    # Each user gets their own isolated session
    user1_add, user1_search = get_sessionized_cognee_tools("user-123")
    user2_add, user2_search = get_sessionized_cognee_tools("user-456")
    
    # Create separate agents for each user
    agent1 = Agent(
        model="gemini-2.5-flash",
        name="assistant_1",
        description="Assistant for user 1",
        instruction="You are a helpful assistant.",
        tools=[user1_add, user1_search]
    )
    
    agent2 = Agent(
        model="gemini-2.5-flash",
        name="assistant_2",
        description="Assistant for user 2",
        instruction="You are a helpful assistant.",
        tools=[user2_add, user2_search]
    )
    
    runner1 = InMemoryRunner(agent=agent1)
    runner2 = InMemoryRunner(agent=agent2)
    
    # Each agent works with isolated data
    await runner1.run_debug("Remember: I like pizza")
    await runner2.run_debug("Remember: I like sushi")

if __name__ == "__main__":
    asyncio.run(main())
```

## Tool Reference

### `add_tool(data: str, node_set: Optional[List[str]] = None)`

Store information in the memory for later retrieval.

**Parameters:**
- `data` (str): The text or information you want to store
- `node_set` (Optional[List[str]]): Additional node set identifiers for organization

**Returns:** Confirmation message

**Example:**
```python
agent = Agent(
    model="gemini-2.5-flash",
    name="data_manager",
    description="Data management specialist",
    instruction="You manage our knowledge base.",
    tools=[add_tool]
)

runner = InMemoryRunner(agent=agent)
await runner.run_debug(
    "Store this: Our Q4 revenue was $2.5M with 15% growth"
)
```

### `search_tool(query_text: str, node_set: Optional[List[str]] = None)`

Search and retrieve previously stored information from the memory.

**Parameters:**
- `query_text` (str): Natural language search query
- `node_set` (Optional[List[str]]): Additional node set identifiers for scoping the search

**Returns:** List of relevant search results

**Example:**
```python
agent = Agent(
    model="gemini-2.5-flash",
    name="research_assistant",
    description="Research specialist",
    instruction="You help users find information quickly.",
    tools=[search_tool]
)

runner = InMemoryRunner(agent=agent)
await runner.run_debug("What was our Q4 revenue?")
```

### `get_sessionized_cognee_tools(session_id: Optional[str] = None)`

Returns cognee tools with optional user-specific sessionization.

**Parameters:**
- `session_id` (Optional[str]): User identifier for data isolation. If not provided, a random session ID is auto-generated.

**Returns:** `(add_tool, search_tool)` - A tuple of sessionized tools

**Example:**
```python
# With explicit session ID
add_tool, search_tool = get_sessionized_cognee_tools("user-123")

# Auto-generate session ID
add_tool, search_tool = get_sessionized_cognee_tools()
```

## Configuration

### Environment Variables

Create a `.env` file in your project root:

```bash
# OpenAI API key (used by Cognee for LLM operations)
LLM_API_KEY=your-openai-api-key-here

# Google API key (used by Google ADK for Gemini models)
GOOGLE_API_KEY=your-google-api-key-here
```

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

Check out the `examples/` directory for comprehensive usage examples:

- **`examples/tools_example.py`**: Basic usage with add and search tools
- **`examples/sessionized_tools_example.py`**: Multi-user session management with visualization

## Advanced Usage

### Pre-loading Data

You can pre-load data into Cognee before creating agents:

```python
import asyncio
import cognee
from cognee_integration_google_adk import search_tool
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner

async def main():
    # Pre-load data
    await cognee.add("Important company information here...")
    await cognee.add("More data to remember...")
    await cognee.cognify()  # Process and index the data
    
    # Now create an agent that can search this data
    agent = Agent(
        model="gemini-2.5-flash",
        name="analyst",
        description="Analyst with access to company knowledge base",
        instruction="You have access to our company knowledge base.",
        tools=[search_tool]
    )
    
    runner = InMemoryRunner(agent=agent)
    events = await runner.run_debug("What information do we have?")
    
    for event in events:
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if part.text:
                    print(part.text)

if __name__ == "__main__":
    asyncio.run(main())
```

### Data Management

```python
import asyncio
import cognee

async def reset_knowledge_base():
    """Clear all data and reset the knowledge base"""
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

async def visualize_knowledge_graph():
    """Generate a visualization of the knowledge graph"""
    await cognee.visualize_graph("graph.html")
```

### Working with Multiple Agents

```python
import asyncio
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from cognee_integration_google_adk import add_tool, search_tool

async def main():
    # Create a data entry agent
    data_agent = Agent(
        model="gemini-2.5-flash",
        name="data_collector",
        description="Collects and stores information",
        instruction="You collect and store important information.",
        tools=[add_tool]
    )
    
    # Create a research agent
    research_agent = Agent(
        model="gemini-2.5-flash",
        name="researcher",
        description="Searches and analyzes stored information",
        instruction="You search and analyze information from the knowledge base.",
        tools=[search_tool]
    )
    
    data_runner = InMemoryRunner(agent=data_agent)
    research_runner = InMemoryRunner(agent=research_agent)
    
    # Store data
    await data_runner.run_debug(
        "Store this: Project Alpha launched in Q1 2024 with $5M budget"
    )
    
    # Search data
    events = await research_runner.run_debug(
        "When did Project Alpha launch and what was the budget?"
    )
    
    for event in events:
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if part.text:
                    print(part.text)

if __name__ == "__main__":
    asyncio.run(main())
```

## Async-first Guidance

All tools in this integration are async (`async def`). Keep the following in mind:

- **Long-running operations:** `cognee.add()` and `cognee.cognify()` can take seconds to minutes depending on data volume. Wrap them with `asyncio.wait_for()` to enforce timeouts in production:
  ```python
  await asyncio.wait_for(cognee.cognify(), timeout=300)
  ```
- **Retries for transient failures:** LLM and network calls can fail intermittently. Use retry logic (e.g., `tenacity`) around `add_tool` and `search_tool` invocations in your agent orchestration layer.
- **Non-blocking deployment:** Do not call Cognee tools from synchronous request handlers. Use `asyncio.run()` or an async web framework to avoid blocking your application.
- **Google ADK long-running tools:** When using ADK's `InMemoryRunner`, agent tool calls are already async. Ensure your runner's timeout is high enough for indexing operations.

## Requirements

- Python 3.10+
- Google API key (for Gemini models via Google ADK)
- OpenAI API key (or other LLM provider supported by Cognee)
- Dependencies automatically managed via pyproject.toml