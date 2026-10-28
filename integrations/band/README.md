# Cognee Memory for Band Agents

Give any Band agent a persistent, shared knowledge graph in one line of code.

**Package:** `cognee-integration-band` (imports as `cognee_band`)
**Repository:** `topoteretes/cognee-integrations`, path `integrations/band/`
**Requires:** Python 3.11 or later, `band-sdk` 6.0.1 or later, a reachable Cognee server

---

## What this is

[Band](https://band.ai) is where agents and humans work together: agents you run yourself connect to Band and share rooms with people and other agents. You build them with the [Band Python SDK](https://pypi.org/project/band-sdk/) by giving an `Agent` an *adapter* that holds your LLM logic (Anthropic, LangGraph, CrewAI, Pydantic AI, and others).

On their own, Band agents keep nothing between rooms or restarts. `CogneeMemoryAdapter` wraps your adapter and connects it to a Cognee knowledge graph, the same graph the Cognee plugins for Claude Code and Codex write to:

- **Auto-recall.** Relevant memory is added above each room message before your adapter sees it.
- **Auto-capture.** Each question and the reply your adapter sent are stored, in the background.
- **Consolidation.** When a room closes or the agent stops, the room's session is turned into graph memory by Cognee's `improve` pipeline.
- **Explicit tools.** Optionally, the model gets `cognee_search` and `cognee_remember`.
- **Fails safe.** If Cognee is down, your agent answers exactly as it would without memory.

---

## Before you start

You need three things:

1. **A Band agent.** Create one at `https://app.band.ai/agents`. Keep its agent id and API key.
2. **A Cognee server.** Cognee Cloud, self-hosted, or local. The integration is a thin client and does not start one for you.
3. **A model provider key** for the adapter you use, for example `ANTHROPIC_API_KEY`.

If you already use the Cognee plugin for Claude Code or Codex, items 2 and 3 are done. This integration reads the same config file.

---

## 1. Install

```bash
pip install "band-sdk[anthropic]>=6.0.1"     # or [langgraph], [crewai], ...
pip install "cognee-integration-band @ git+https://github.com/topoteretes/cognee-integrations#subdirectory=integrations/band"
```

Pick the band-sdk extra matching the adapter you plan to wrap. The memory package depends only on `band-sdk`; its Cognee client uses the standard library.

Working from a clone of the repo instead:

```bash
git clone https://github.com/topoteretes/cognee-integrations
cd cognee-integrations
pip install -e integrations/band
```

---

## 2. Configure

Point at your Cognee server once, in `~/.cognee/.env`:

```bash
mkdir -p ~/.cognee
cat >> ~/.cognee/.env <<'EOF'
COGNEE_BASE_URL="https://your-instance.cognee.ai"
COGNEE_API_KEY="ck_..."
EOF
chmod 600 ~/.cognee/.env
```

Same file the Claude Code and Codex plugins read. If you have it already, skip this.

Precedence is `exported env vars` > `~/.cognee/.env` > `defaults`. A shell export always wins, which is what you want in CI and containers.

### Settings

| Variable | Default | Effect |
|---|---|---|
| `COGNEE_BASE_URL` | `http://localhost:8011` | Cognee server URL |
| `COGNEE_API_KEY` | unset | sent as the `X-Api-Key` header |
| `COGNEE_PLUGIN_DATASET` | `agent_sessions` | dataset to read and write |
| `COGNEE_RECALL_TOP_K` | `5` | results per recall |
| `COGNEE_ENV_FILE` | `~/.cognee/.env` | override the config file location |

Running Cognee locally on the default port needs no configuration at all.

### Band credentials

Keep these in your shell, not in the Cognee env file:

```bash
export BAND_AGENT_ID="..."
export BAND_AGENT_API_KEY="..."
export ANTHROPIC_API_KEY="sk-ant-..."
```

---

## 3. Wrap your adapter

One line:

```python
import asyncio
import os

from band import Agent
from band.adapters import AnthropicAdapter
from cognee_band import CogneeMemoryAdapter


async def main() -> None:
    agent = Agent.create(
        adapter=CogneeMemoryAdapter(AnthropicAdapter()),   # was: AnthropicAdapter()
        agent_id=os.environ["BAND_AGENT_ID"],
        api_key=os.environ["BAND_AGENT_API_KEY"],
    )
    await agent.run()


asyncio.run(main())
```

`CogneeMemoryAdapter(inner)` with no other arguments resolves settings from the environment.

Your adapter, prompts, and framework do not change. The wrapper accepts any band-sdk adapter built on `SimpleAdapter`, which covers every built-in adapter. It composes your adapter the same way band-sdk's own `SlackAdapter` does: your adapter keeps its features, model selection, control handling, and resource cleanup, and the Band runtime starts and stops it exactly as it would unwrapped.

### With explicit memory tools

To let the model also make deliberate memory calls, build one client and share it:

```python
from band.adapters import AnthropicAdapter, AnthropicAdapterConfig
from cognee_band import CogneeClient, CogneeMemoryAdapter, CogneeSettings, cognee_tools

settings = CogneeSettings.resolve()
client = CogneeClient(settings)

inner = AnthropicAdapter(
    AnthropicAdapterConfig(
        custom_section=(
            "You are a helpful assistant with persistent memory. Blocks labeled "
            "'Cognee memory' contain context recalled from past sessions, so treat "
            "them as your own memory. Use cognee_search for explicit lookups and "
            "cognee_remember to store facts worth keeping."
        ),
    ),
    additional_tools=cognee_tools(client),
)

agent = Agent.create(
    adapter=CogneeMemoryAdapter(inner, settings=settings, client=client),
    agent_id=os.environ["BAND_AGENT_ID"],
    api_key=os.environ["BAND_AGENT_API_KEY"],
)
```

Sharing one `settings` and one `client` keeps the tools and the wrapper on the same dataset.

Include the prompt section. Without it the model may treat the injected block as something the user said instead of recalled memory. `cognee_tools` works with any adapter that accepts `additional_tools`, including Anthropic, Claude Agent SDK, LangGraph, Pydantic AI, CrewAI, Gemini, Google ADK, and Strands.

### Run the example

```bash
uv run examples/memory_agent.py
```

`examples/memory_agent.py` is a full working agent with memory and tools. Its docstring lists every credential it needs, and its inline script metadata installs `band-sdk` and this package for you.

---

## 4. Verify it works

**Check startup.** With logging on you should see:

```
cognee memory active: dataset=agent_sessions server=https://your-instance.cognee.ai
```

A wrong server shows up here, not three turns later.

**Check the round trip.**

1. In a room: "Remember that the staging database migration is owned by Ravit."
2. Close that room, or wait a minute for Cognee to finish building the graph in the background.
3. In a **new room**: "Who owns the staging migration?"

Use a new room. Same-room recall could just be conversation history.

**Check the cross-harness path.** Tell the Cognee Claude Code plugin a fact in a terminal session, let it sync, then ask your Band agent in a room. Both default to the `agent_sessions` dataset, so the answer comes back. This is the demo worth showing someone.

---

## 5. What happens on every turn

| When | What happens |
|---|---|
| Agent starts | your adapter starts as usual, then the active dataset and server are logged |
| A text message arrives | recalls context and injects it above the message as a labeled block |
| Your adapter replies | stores the question and reply as a QA pair, in the background |
| A room closes | waits for that room's pending writes, then bridges its session into the graph via Cognee's `improve` pipeline |
| The agent stops | your adapter shuts down, pending writes drain, then every still-open room is bridged |

Each Band room maps to one Cognee session named `band-<room id>`. Room ids are UUIDs, so in Cognee you will see sessions like `band-3f1c2a9e-…`. A room becomes durable memory instead of a transcript that disappears.

Graph building runs in the background on the Cognee server, so a fact becomes recallable from other rooms shortly after the room is bridged, not instantly.

Non-text events (`tool_call`, `thought`, and others) pass through untouched and are never stored.

### What the model sees

```
Cognee memory — context recalled from shared memory, possibly relevant to the message below:
- the deployment target is EKS, decided last Tuesday
- Ravit owns the migration checklist

---

what's left on the migration?
```

When recall finds nothing, nothing is injected and the model sees only the original message.

---

## 6. Choosing your memory scope

`COGNEE_PLUGIN_DATASET` is the isolation control. Recall reads exactly one dataset, so it is a hard boundary.

| You want | Set |
|---|---|
| One shared brain across all agents and coding harnesses | leave the default `agent_sessions` |
| Isolation per team or product | one dataset name per team |
| Full isolation per agent | one dataset name per agent |

Decide before agents start writing. Moving content between datasets afterwards is a Cognee-side job, not something the integration does.

Recalled memory goes into the model prompt. Whatever is in the dataset can reach your model provider, so pick a shared dataset deliberately.

---

## 7. Troubleshooting

**`TypeError: CogneeMemoryAdapter wraps a band-sdk SimpleAdapter`**
You passed something other than a band-sdk adapter instance, or an adapter from band-sdk older than 6.0.1. Upgrade with `pip install -U "band-sdk>=6.0.1"`.

**Nothing is recalled.**
Check for a warning on the `cognee_band` logger or a `[cognee-band]` line on stderr. The integration is quiet in normal operation and loud on failure. No warning means the server searched and found nothing, which is expected on a cold dataset or right after a room was bridged.

**`server unreachable at ...`**
Transport failure. Check that `COGNEE_BASE_URL` is reachable from the agent process. Your agent keeps working without memory meanwhile.

**`unauthorized (HTTP 401)` or `(HTTP 403)`**
Set or fix `COGNEE_API_KEY`. Recent Cognee versions enforce auth even on localhost, so a local server may still need a key.

**Configuration seems ignored.**
Env file parsing never raises, so a malformed file is skipped silently. Check the format is `KEY=VALUE`, one per line, optional `export ` prefix, optional quotes. Check nothing in your shell already exports the same variable, since exports win. Check `COGNEE_ENV_FILE` is not pointing somewhere unexpected.

**TLS or certificate errors.**
Usually a macOS Python install missing root CAs. Run `pip install certifi` in the agent environment. The client picks it up automatically.

**Questions stored with empty answers.**
Replies are captured by proxying `tools.send_message`. An adapter that emits output only through `send_event` is not captured. Check how your inner adapter sends output.

**Memory from a closed room never shows up later.**
Consolidation runs through Cognee's `improve` pipeline when the room closes or the agent stops, in the background server-side. Give it time. Also confirm the agent shut down cleanly, since that is what bridges rooms that were still open.

**Slower first response.**
Recall runs before your adapter sees the message and is the only memory operation on the critical path. Default timeout is 20 seconds. Lower it if you would rather drop memory than wait:

```python
settings = CogneeSettings.resolve(recall_timeout=5.0)
```

---

## 8. Advanced

### Override settings in code

```python
settings = CogneeSettings.resolve(
    dataset="team_platform",
    top_k=8,
    recall_timeout=5.0,
    session_prefix="prod",       # room <id> -> session prod-<id>
)
```

Any field on `CogneeSettings` can be overridden. Explicit overrides beat both the environment and the config file.

### Run tests

```bash
cd integrations/band
uv run --with pytest --with pytest-asyncio python -m pytest tests/ -q
```

The tests run against the real band-sdk types and runtime startup and disposal functions, so a band-sdk change that affects the wrapper fails here first.

### For Claude Code users

Band's Jam Desktop bridges local Claude Code sessions onto Band. Those are real Claude Code sessions, so the existing Cognee Claude Code plugin already gives them memory. This package is for agents you build on the Band SDK directly.

---

## 9. FAQ

**Does this work with my framework?**
Yes, if your adapter is a band-sdk adapter built on `SimpleAdapter`, which every built-in adapter is. The wrapper composes it the way band-sdk's own `SlackAdapter` does, so the Band runtime runs your adapter's full startup and cleanup.

**What if Cognee goes down?**
Your agent answers exactly as it would without the wrapper. Every memory failure is logged and swallowed.

**Does memory slow the agent down?**
Writes do not. They run in the background, off the event loop. Recall does add latency before the turn starts, since the result is needed before your adapter runs.

**Can several agents share one memory?**
Yes, and they do by default. Any number of agents on the same dataset share one graph.

**Is my content sent anywhere new?**
Question and reply text goes to your Cognee server, and recalled memory goes into the prompt sent to your model provider. With both Band and Cognee self-hosted, nothing leaves your network except the model provider call your adapter was already making.

**Are tool calls remembered?**
Not currently. Only `text` messages get memory treatment. The client has a `store_trace` method for this, but the adapter does not drive it yet.

---

## Links

- Integration: [topoteretes/cognee-integrations](https://github.com/topoteretes/cognee-integrations), `integrations/band/`
- Band: [band.ai](https://band.ai) | Agent console: `https://app.band.ai/agents` | SDK: [band-sdk on PyPI](https://pypi.org/project/band-sdk/)
- Cognee: [cognee.ai](https://www.cognee.ai)
