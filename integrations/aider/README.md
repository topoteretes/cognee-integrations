# Cognee Integration for Aider

Adds Cognee-backed project memory tools for
[Aider](https://aider.chat/), the terminal-native, multi-model coding assistant.

This starter integration keeps the Aider surface simple: it provides a small CLI
and Python helpers that Aider can call from terminal workflows to store and
retrieve project memory. The integration scopes memory by local repository by
default, so separate projects do not share context accidentally.

## Features

- `cognee_remember`: store project decisions, architecture notes, and developer intent.
- `cognee_search`: retrieve relevant graph/session memory for a question.
- Project-scoped session IDs derived from the current repository path.
- Config precedence: environment variables, project config, defaults.
- JSON tool specs that can be injected into Aider prompts or wrapper workflows.
- Local-first tests that do not require paid model endpoints.

## Installation

From this repository:

```bash
cd integrations/aider
uv sync --dev
```

When published:

```bash
pip install cognee-integration-aider
```

## Configuration

By default, the integration reads project config from `.aider/cognee.json` in the
current working tree. Override the path with `AIDER_COGNEE_CONFIG`.

Precedence for every setting is:

1. environment variables
2. `.aider/cognee.json`
3. defaults

| Setting | Env var | Default |
| --- | --- | --- |
| `dataset` | `COGNEE_DATASET` | `aider` |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `aider` |
| `project_id` | `COGNEE_PROJECT_ID` | derived from repo path |
| `top_k` | `COGNEE_TOP_K` | `5` |
| `self_improvement` | `COGNEE_SELF_IMPROVEMENT` | `false` |
| `service_url` | `COGNEE_BASE_URL` | empty |
| `api_key` | `COGNEE_API_KEY` | empty |
| `data_root` | `COGNEE_DATA_ROOT` | empty |
| `system_root` | `COGNEE_SYSTEM_ROOT` | empty |

Example `.aider/cognee.json`:

```json
{
  "dataset": "my-service",
  "session_prefix": "aider",
  "top_k": 8,
  "self_improvement": false
}
```

## CLI Usage

Store a note:

```bash
cognee-aider remember "We use FastAPI dependency injection for service wiring."
```

Search memory:

```bash
cognee-aider search "How is dependency injection handled?"
```

Show the current scoped session:

```bash
cognee-aider session
```

Print the tool contract as JSON:

```bash
cognee-aider specs
```

## Aider Workflow

Aider can use shell commands in a project workflow. A simple pattern is to keep
the Cognee tools available as commands and ask Aider to call them when it needs
project memory:

```txt
Use `cognee-aider search "<question>"` before answering questions about prior
project decisions. Use `cognee-aider remember "<fact>"` when I ask you to store
durable project memory.
```

The Python API exposes the same behavior for custom wrappers:

```python
from cognee_integration_aider import build_session_id, load_config
from cognee_integration_aider.tools import cognee_tool_specs

config = load_config()
session_id = build_session_id(config)
tool_specs = cognee_tool_specs()
```

## Development

```bash
cd integrations/aider
uv sync --dev
uv run pytest -q
uv run ruff check .
```
