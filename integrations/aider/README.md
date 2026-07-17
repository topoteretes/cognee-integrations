# Cognee memory for Aider

Persistent, per-project memory for terminal developers who use
[Aider](https://github.com/Aider-AI/aider), powered by
[Cognee](https://github.com/topoteretes/cognee). Store project context,
decisions, and logs and recall them across disconnected sessions — with
OpenAI, any OpenAI-compatible API, or fully local models via Ollama.

## What it provides

Two sessionized async tools, where a **session** is one project and each
session gets its own isolated Cognee scope (a dataset tagged with a per-session
node set, so one project's recall never surfaces another's):

- `add_project_memory(session, content)` — store `content` and build the
  knowledge graph for that session.
- `search_project_memory(session, query)` — recall relevant memories, scoped
  to that session only.

`get_sessionized_cognee_tools(session)` returns `(add, search)` already bound
to a session, so callers don't thread the session id through every call.

## Install

```bash
git clone https://github.com/topoteretes/cognee-integrations.git
cd cognee-integrations/integrations/aider
uv sync
```

## Configure

Copy the example env file and add your key. It defaults to OpenAI (cognee's
default provider); uncomment the Ollama block to run locally and for free via
[Ollama](https://ollama.com/) (`ollama serve`). The package loads this `.env`
automatically on import.

```bash
cp .env.example .env
```

## Usage

The tools are plain importable async functions, so you can call them from any
Python entry point — including an Aider [Python-scripting](https://aider.chat/docs/scripting.html)
script that wraps an Aider `Coder`:

```python
import asyncio
from cognee_integration_aider import get_sessionized_cognee_tools

remember, recall = get_sessionized_cognee_tools("my-project")

async def main():
    await remember("We decided to use PostgreSQL with pgvector.")
    print(await recall("What database did we choose?"))

asyncio.run(main())
```

> **Note on Aider integration.** Aider does not currently expose a supported
> mechanism to register custom Python tools for its own tool-calling loop, so
> these are used from Python (as above) rather than auto-invoked by Aider. If
> Aider adds such a hook, the same functions can be wired straight into it.

To use them *inside* an Aider coding session, wire them around Aider's
[scripting API](https://aider.chat/docs/scripting.html) — recall context,
run the `Coder`, store the outcome. `examples/aider_with_memory.py` shows the
full pattern (needs `pip install aider-chat` and a configured model).

## Run the examples

```bash
# memory + per-project isolation (cognee only)
uv run python examples/aider_memory_demo.py

# memory wired into a real Aider coding session (needs: pip install aider-chat)
python examples/aider_with_memory.py
```

## Test

The suite mocks Cognee, so it runs offline with no API keys:

```bash
uv run pytest
```

## Attribution

Originally contributed by [@jaya6400](https://github.com/jaya6400) in
cognee-integrations #198.
