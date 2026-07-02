# Cognee Graph Memory Integration for Aider CLI

This module provides persistent, native graph memory support for [Aider](https://github.com/Aider-AI), the leading multi-model terminal coding assistant. It allows terminal-bound developers utilizing OpenAI, DeepSeek, or local models via Ollama to cleanly retain architectural context, disjointed session logs, and project decisions across isolated workspaces.

## Structure Overview
- `cognee_integration_aider/config.py`: Handles path sandboxing and `env > config > defaults` priority structures.
- `cognee_integration_aider/tools.py`: Wraps Cognee graph manipulation methods into standalone JSON-serializable tool hooks.

## Installation
From your project environment root, synchronize your integration package layout using `uv`:
```bash
uv sync
```

## Environment Setup
Copy the example environment file and fill in your Cognee settings:
```bash
cp .env.example .env
```

The example values in `.env.example` are:
```env
COGNEE_SERVICE_URL=http://localhost:8000
COGNEE_API_KEY=mock-api-key-1234
COGNEE_MEMORY_MODE=local
```

## Running Verification Tests
Execute the comprehensive offline test suite to verify package imports, memory helpers, and session ID formatting:
```bash
cd integrations/aider
PYTHONPATH=. uv run --active pytest tests/ -v
```

## Running the Example Script
Run the example demo to validate the package in the expected runtime shape:
```bash
cd integrations/aider
PYTHONPATH=. uv run --active python examples/aider_memory_demo.py
```

## Final Fix Summary
- Added package metadata and setuptools discovery to `integrations/aider/pyproject.toml`
- Added `cognee_integration_aider/__init__.py` for proper package import
- Registered the module cleanly in `integrations/inventory.yml`
- Added `.env.example` with mock Cognee credentials and default mode
- Confirmed relative imports are correct and no absolute system paths are used

## Using with Aider CLI
Expose these functions inside your Aider runtime loop or register them using Aider's background `--tool` parameters:
- `add_project_memory(session_id, content)`: Ingest design details or workspace changes directly into the Cognee graph layer.
- `search_project_memory(session_id, query)`: Recall multi-session project logs or constraints inside your active chat window.
