# Cognee for Pydantic AI

`cognee-integration-pydantic-ai` gives a Pydantic AI agent three async memory tools: `cognee_remember`, `cognee_search`, and `cognee_forget`. It uses the Cognee Python SDK directly, without a separate MCP server.

## Install

```bash
cd integrations/pydantic-ai
pip install .
```

Once published, the package can also be installed as `pip install cognee-integration-pydantic-ai`. Configure Cognee's usual `LLM_API_KEY` and model settings for real memory writes and graph recall. For the example agent, also configure `OPENAI_API_KEY` (it may be the same key).

## Use

```python
from pydantic_ai import Agent
from cognee_integration_pydantic_ai import cognee_toolset

agent = Agent(
    "openai:gpt-4o-mini",
    toolsets=[cognee_toolset(user_id="customer-42", auto_recall=True)],
)
result = agent.run_sync("Remember that this customer prefers email updates.")
print(result.output)
```

See [`examples/agent.py`](examples/agent.py) for a runnable two-agent example. `auto_recall=True` searches Cognee using the current text prompt before each run and adds matching memory as dynamic Pydantic AI instructions. This keeps recall fresh when message history is reused. For non-text prompts, use `cognee_search` explicitly. Omit `auto_recall` to use tools only.

## Scope and deletion

Use `user_id`, `session_id`, or both. Each distinct scope gets a separate Cognee dataset and a node-set tag for local graph organization. Dataset scoping keeps search and `cognee_forget` inside that scope, including when using Cognee's remote SDK (which currently does not forward `node_set` on writes). With only `user_id`, memory persists across that user's sessions; with both IDs, it belongs to that single user/session pair. With neither, all agents using the same base dataset share memory. `cognee_forget` **deletes the entire configured scope**, so expose that tool only to agents allowed to erase it.

Options may also come from environment variables: `COGNEE_PYDANTIC_AI_DATASET` (base dataset, default `pydantic_ai`), `COGNEE_PYDANTIC_AI_USER_ID`, and `COGNEE_PYDANTIC_AI_SESSION_ID`. Explicit arguments take precedence. Scope identifiers are hashed in dataset and node-set names; raw user IDs are not placed in those names.

The integration fails open when Cognee is unavailable: tools return an availability message and automatic recall adds no context. Failures are logged. A real LLM key and configured Cognee backend are needed for live ingest and recall; the included tests use Pydantic AI's `FunctionModel` and mocked Cognee calls.

## Development

```bash
uv sync --locked --dev
uv run pytest tests/ -v
uv run ruff check .
```
