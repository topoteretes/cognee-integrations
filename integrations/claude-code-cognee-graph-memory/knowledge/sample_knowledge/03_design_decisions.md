# Design decisions

## Why Cognee for the graph memory engine

Cognee was chosen because the graph DB (Ladybug DB since Cognee 1.0.4, KuzuDB up
to 1.0.3), vector DB (LanceDB), and embedding model (FastEmbed) are all bundled
with no extra installation, and everything runs fully locally. This satisfies
the no-external-API-key and zero-additional-cost requirements.

## Why MCP scope=user

The Cognee MCP server is registered with `scope=user`. With `scope=project`,
only that one project's Claude Code session could access it. Cross-project
access is the whole point of this graph memory system.

## Why Ollama + qwen2.5:14b for the LLM

Ollama with qwen2.5:14b runs fully locally, needs no external API key, and
reuses an existing Ollama installation. It is accurate enough for entity
extraction and incurs no additional cost. Among local LLMs, qwen2.5:14b is the
only one verified to satisfy Cognee's structured-output requirements (perfect
20/20 score in the v0.1.x verification matrix).

## Why stdio mode for transport

stdio mode is used to communicate with Claude Code because it does not consume
a port (no clashes with other services), and Claude Code launches the MCP
server directly as a child process — no separate HTTP server required.
