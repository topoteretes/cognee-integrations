# Cognee Memory for Band

Persistent, shared Cognee memory for [Band](https://band.ai) agents.

## What is this?

Band is a hosted multi-agent communication platform: agents you run yourself
connect to `app.band.ai` and talk to humans and other agents in chat rooms.
Agents are built with the [Band Python SDK](https://pypi.org/project/band-sdk/)
by composing an `Agent` (the platform connection) with an *adapter* (your LLM
logic — LangGraph, Anthropic, CrewAI, ...).

Band agents are stateless between rooms and restarts. This package gives them
a memory: `CogneeMemoryAdapter` wraps any Band adapter — one line of code —
and connects it to a Cognee knowledge graph, the same way the Cognee plugins
for Claude Code, Codex, and OpenClaw connect those harnesses.

```python
agent = Agent.create(
    adapter=CogneeMemoryAdapter(AnthropicAdapter(...)),   # was: AnthropicAdapter(...)
    agent_id=..., api_key=...,
)
```

## What you get

- **One memory across all your agents.** By default memory lives in the
  `agent_sessions` dataset — the same one the Claude Code, Codex, and OpenClaw
  plugins use. Tell Claude Code something in your terminal, then ask your Band
  agent about it in a chat room, and it knows. Any number of Band agents can
  share the same memory, or be isolated per dataset.
- **Auto-recall, zero prompting.** Before each room message reaches your
  adapter, relevant context is recalled from Cognee and injected as a labeled
  block above the message. The model sees past decisions, facts, and
  preferences without anyone asking it to look.
- **Auto-capture, zero effort.** Every incoming message and the reply your
  adapter sent are stored as a QA pair in the Cognee session cache —
  fire-and-forget, off the event loop, so turns are never slowed down.
- **Long-term consolidation.** When a room closes or the agent stops, the
  session is bridged into graph memory via Cognee's `/improve` pipeline
  (feedback weighting, distillation, enrichment) — raw chat turns become
  durable, queryable knowledge.
- **Explicit memory tools.** Optionally expose `cognee_search` and
  `cognee_remember` to the model, so it can deliberately look something up or
  pin a fact worth keeping.
- **Memory that can't break your agent.** Every memory failure is logged and
  swallowed; if the Cognee server is down, your agent answers exactly as it
  would without the wrapper.
- **Framework-agnostic.** The wrapper imports nothing from `band` and drives
  the inner adapter through the same duck-typed surface the Band `Agent` uses,
  so it works with any current or future Band adapter unchanged.

## Install

Install from this repo, plus the Band SDK with the adapter extra you use:

```bash
pip install "band-sdk[anthropic]"        # or [langgraph], [crewai], ...
pip install -e integrations/band         # from the repo root
```

The Band SDK requires Python >= 3.11.

## Configure

Point at a running Cognee server — **once** — in `~/.cognee/.env` (the same
file the Claude Code and Codex plugins read; real shell exports still win):

```bash
mkdir -p ~/.cognee
cat >> ~/.cognee/.env <<'EOF'
COGNEE_BASE_URL="https://your-instance.cognee.ai"
COGNEE_API_KEY="ck_..."
EOF
chmod 600 ~/.cognee/.env
```

The integration is a pure thin client: it needs a reachable Cognee server (Cognee
Cloud, or a self-hosted/local one — default `http://localhost:8011`) and does
not bootstrap a local runtime.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_BASE_URL` | `http://localhost:8011` | Cognee server |
| `COGNEE_API_KEY` | unset | API key (`X-Api-Key`) |
| `COGNEE_PLUGIN_DATASET` | `agent_sessions` | Dataset for writes and recall |
| `COGNEE_RECALL_TOP_K` | `5` | Results per recall |

## Use

Wrap your adapter, and optionally add the explicit tools:

```python
from band import Agent
from band.adapters import AnthropicAdapter
from cognee_band import CogneeClient, CogneeMemoryAdapter, CogneeSettings, cognee_tools

settings = CogneeSettings.resolve()
client = CogneeClient(settings)

inner = AnthropicAdapter(additional_tools=cognee_tools(client))
agent = Agent.create(
    adapter=CogneeMemoryAdapter(inner, settings=settings, client=client),
    agent_id="...",  # from https://app.band.ai/agents
    api_key="...",
)
await agent.run()
```

`CogneeMemoryAdapter(inner)` with no arguments also works — settings resolve
from the environment. A complete runnable example (agent + memory + tools) is
in [`examples/memory_agent.py`](./examples/memory_agent.py); its docstring
lists the required credentials.

The headline demo, once you have credentials: tell the Cognee Claude Code
plugin a fact in a terminal session, let it sync, then ask the Band agent —
both write to and recall from the same `agent_sessions` dataset.

## How it works

The wrapper intercepts the adapter lifecycle the Band `Agent` drives, mapping
it onto the same memory flow the Claude Code plugin implements with hooks:

| Band adapter hook | Claude Code equivalent | Cognee action |
|---|---|---|
| `on_started` | `SessionStart` | log active dataset/server (delegates first) |
| `on_event` (per room message) | `UserPromptSubmit` + `Stop` | recall → inject `Cognee memory` block into `msg.content`; after the inner adapter runs, store the QA pair via `/api/v1/remember/entry` |
| `on_cleanup(room_id)` | `SessionEnd` | bridge the room's session into the graph via `/api/v1/improve` |
| `cleanup_all` (agent stop) | final sync worker | drain pending stores, then improve every active room session |

Sessions map 1:1 to Band rooms: room `r1` → Cognee session `band-r1`, so a
room is a durable conversation memory. Replies are captured by proxying
`tools.send_message` (Band adapters send output explicitly rather than
returning it). Only `text` messages get memory treatment; `tool_call`,
`thought`, and other event types pass through untouched. An HTTP error from a
reachable server is never misread as "no results" (same contract as the
Claude Code plugin).

Note for Claude Code users: Band's Jam Desktop bridges local Claude Code
sessions onto Band — those are real Claude Code sessions, so the existing
[Cognee Claude Code plugin](../claude-code/) already gives them memory; this
package is for agents built on the Band SDK directly.

## Test

```bash
cd integrations/band
uv run --with pytest --with pytest-asyncio --with pydantic python -m pytest tests/ -q
```
