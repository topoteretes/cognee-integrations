# Cognee memory for Deep Agents

[Deep Agents](https://github.com/langchain-ai/deepagents) plans with `write_todos`,
works in a virtual filesystem, and hands sub-tasks to sub-agents. Its long-term
memory is what the agent chooses to write down under `/memories/`; everything it
*did* — the plan, the tool calls, what failed and why, what each sub-agent
reported — is gone when the thread ends.

This integration plugs Deep Agents into [cognee](https://github.com/topoteretes/cognee)
so every step is remembered and every new run starts with what the last one learned.

## What it does

- **`CogneeTraceMiddleware`** records every tool call (planning via `write_todos`,
  sub-agent hand-offs via `task`, file operations, custom tools) as a cognee
  `TraceEntry`, and the turn's request/answer as a `QAEntry`, in the session cache
  keyed by the LangGraph `thread_id`. Trace writes cost no LLM calls — they land in
  the fast session cache; `improve()` bridges them into the graph later.
- **Before each run** the middleware recalls what cognee knows about the request —
  graph memory plus earlier sessions of the same thread, lessons included — and
  injects it as a system message.
- **`with_cognee_tracing(subagents)`** copies each `SubAgent` spec with the
  middleware added (declarative sub-agents do not inherit middleware from
  `create_deep_agent`), so their steps land in the same session.
- **`persist_thread(thread_id)`** runs `cognee.improve` for a thread explicitly at
  its end; cognee's own background debounce covers the rest.
- **`CogneeBackend`** mirrors the agent's `/memories/` files into cognee: writes are
  remembered into the dataset, deletes are forgotten. The wrapped backend stays the
  source of truth, so the agent always reads back exactly what it wrote.

Nothing cognee does may break the agent: every cognee call is wrapped, and a failed
write is logged while the tool result passes through unchanged. A `redact` hook runs
before anything is stored, results are truncated past `max_result_chars`, and
`exclude_tools` skips tools entirely.

## Install

```bash
uv add cognee-integration-deepagents
# or
pip install cognee-integration-deepagents
```

Configure cognee as usual (`.env` with your `LLM_PROVIDER`/`LLM_MODEL`/embedding
settings — see the [cognee docs](https://docs.cognee.ai)).

## Usage

```python
from deepagents import create_deep_agent
from cognee_integration_deepagents import CogneeTraceMiddleware, with_cognee_tracing

agent = create_deep_agent(
    model=model,
    tools=tools,
    subagents=with_cognee_tracing(subagents),
    middleware=[CogneeTraceMiddleware(dataset_name="research_agent")],
)
```

With `/memories/` mirrored into cognee as well:

```python
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend
from deepagents.backends.state import StateBackend
from cognee_integration_deepagents import CogneeBackend, CogneeTraceMiddleware

agent = create_deep_agent(
    model=model,
    tools=tools,
    middleware=[CogneeTraceMiddleware(dataset_name="research_agent")],
    backend=CompositeBackend(
        default=StateBackend(),
        routes={"/memories/": CogneeBackend("research_agent")},
    ),
)
```

At the end of a thread:

```python
from cognee_integration_deepagents import persist_thread

await persist_thread(thread_id, dataset_name="research_agent")
```

## Secrets and size

Tool arguments and results can hold tokens or large file contents. Pass `redact=...`
to transform text before storage, set `max_result_chars` (default 4000) to cap stored
results, and list tools in `exclude_tools` to skip them entirely.
