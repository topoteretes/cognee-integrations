# Cognee-Integration-CrewAI

A powerful integration between Cognee and CrewAI that provides intelligent knowledge management and retrieval capabilities for AI agents.

## Overview

`cognee-integration-crewai` combines Cognee's advanced knowledge storage and retrieval system with CrewAI's agent framework. This integration allows you to build AI agents that can efficiently store, search, and retrieve information from a persistent knowledge base.

## Features

- **Smart Knowledge Storage**: Add and persist information using Cognee's advanced indexing
- **Semantic Search**: Retrieve relevant information using natural language queries
- **Session Management**: Support for user-specific data isolation
- **CrewAI Integration**: Seamless integration with CrewAI's agent framework
- **Async Support**: Built with async/await for high-performance applications
- **Thread-Safe**: Optimized background event loop for concurrent operations

## Installation

```bash
pip install cognee-integration-crewai
```

## Quick Start

```python
import asyncio
from dotenv import load_dotenv
import cognee
from crewai import Agent
from cognee_integration_crewai import add_tool, search_tool

load_dotenv()

async def main():
    # Initialize Cognee (optional - for data management)
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    
    # Create an agent with memory capabilities
    agent = Agent(
        role="Research Analyst",
        goal="Find and analyze information using the knowledge base",
        backstory="You are an expert analyst with access to a comprehensive knowledge base.",
        tools=[add_tool, search_tool],
        verbose=True
    )
    
    # Use the agent to store information
    response = agent.kickoff(
        "Remember that our company signed a contract with HealthBridge Systems "
        "in the healthcare industry, starting Feb 2023, ending Jan 2026, worth £2.4M"
    )
    print(response.raw)
    
    # Query the stored information
    response = agent.kickoff(
        "What contracts do we have in the healthcare industry?"
    )
    print(response.raw)

if __name__ == "__main__":
    asyncio.run(main())
```

## Available Tools

### Basic Tools

```python
from cognee_integration_crewai import add_tool, search_tool

# add_tool: Store information in the knowledge base
# search_tool: Search and retrieve previously stored information
```

### Sessionized Tools

For multi-user applications, use sessionized tools to isolate data between users:

```python
from cognee_integration_crewai import get_sessionized_cognee_tools

# Get tools for a specific user session
add_tool, search_tool = get_sessionized_cognee_tools("user-123")

# Auto-generate a session ID
add_tool, search_tool = get_sessionized_cognee_tools()
```

## Session Management

`cognee-integration-crewai` supports user-specific sessions to isolate data between different users or contexts:

```python
import asyncio
from crewai import Agent
from cognee_integration_crewai import get_sessionized_cognee_tools

async def main():
    # Each user gets their own isolated session
    user1_add, user1_search = get_sessionized_cognee_tools("user-123")
    user2_add, user2_search = get_sessionized_cognee_tools("user-456")
    
    # Create separate agents for each user
    agent1 = Agent(
        role="Assistant",
        goal="Help user 1",
        backstory="You are a helpful assistant.",
        tools=[user1_add, user1_search]
    )
    
    agent2 = Agent(
        role="Assistant",
        goal="Help user 2",
        backstory="You are a helpful assistant.",
        tools=[user2_add, user2_search]
    )
    
    # Each agent works with isolated data
    response1 = agent1.kickoff("Remember: I like pizza")
    response2 = agent2.kickoff("Remember: I like sushi")

if __name__ == "__main__":
    asyncio.run(main())
```

## Tool Reference

### `add_tool(data: str, node_set: Optional[List[str]] = None)`

Store information in the knowledge base for later retrieval.

**Parameters:**
- `data` (str): The text or information you want to store
- `node_set` (Optional[List[str]]): Additional node set identifiers for organization

**Returns:** Confirmation message

**Example:**
```python
agent = Agent(
    role="Data Manager",
    goal="Store important information",
    backstory="You manage our knowledge base.",
    tools=[add_tool]
)

response = agent.kickoff(
    "Store this: Our Q4 revenue was $2.5M with 15% growth"
)
```

### `search_tool(query_text: str)`

Search and retrieve previously stored information from the knowledge base.

**Parameters:**
- `query_text` (str): Natural language search query

**Returns:** List of relevant search results

**Example:**
```python
agent = Agent(
    role="Research Assistant",
    goal="Find information from our knowledge base",
    backstory="You help users find information quickly.",
    tools=[search_tool]
)

response = agent.kickoff(
    "What was our Q4 revenue?"
)
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
cp .env.template .env
```

Then edit the `.env` file with your API keys:

```env
OPENAI_API_KEY=your-openai-api-key-here
LLM_API_KEY=your-openai-api-key-here
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
- **`examples/sessionized_tools_example.py`**: Multi-user session management

## Advanced Usage

### Pre-loading Data

You can pre-load data into Cognee before creating agents:

```python
import asyncio
import cognee
from cognee_integration_crewai import search_tool
from crewai import Agent

async def main():
    # Pre-load data
    await cognee.add("Important company information here...")
    await cognee.add("More data to remember...")
    await cognee.cognify()  # Process and index the data
    
    # Now create an agent that can search this data
    agent = Agent(
        role="Analyst",
        goal="Answer questions using pre-loaded data",
        backstory="You have access to our company knowledge base.",
        tools=[search_tool]
    )
    
    response = agent.kickoff("What information do we have?")
    print(response.raw)

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

## Requirements

- Python 3.10+
- OpenAI API key (or other LLM provider supported by CrewAI)
- Dependencies automatically managed via pyproject.toml