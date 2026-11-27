# cognee-integration-discord

A Discord bot that gives community/support servers **persistent memory** backed
by [cognee](https://github.com/topoteretes/cognee). It ingests channel messages
into a per-server knowledge graph and answers questions with **citations** back
to the source messages.

> **Design note.** All memory logic lives behind a thin `ChatMemoryAdapter`
> (see cognee issue #3608) so Discord/Slack/Telegram bots can share one memory
> model. This branch ships the **HTTP-client** adapter (`CogneeHttpAdapter`,
> talks to a running cognee server over `/api/v1/remember|recall|forget`). A
> sibling branch provides an in-process SDK adapter instead.

## Memory model

| Discord | cognee | Why |
|---|---|---|
| Server (guild) | dataset (`discord-guild-<id>`) | hard isolation — one server's memory can't leak into another |
| Channel / thread | session (`discord-<guild>-<channel>`) | fast conversational recall scoped to the channel |
| Message | remembered document + provenance header | citations link answers back to the exact message |

## Slash commands

| Command | Who | Effect |
|---|---|---|
| `/cognee-enable` | server admin (`Manage Server`) | opt this channel in to memory capture (nothing is captured until enabled) |
| `/cognee-disable` | server admin | stop capturing this channel |
| `/cognee-ask <question>` | anyone | answer from the server's memory, with a **Sources** footer of message links |
| `/cognee-forget` | server admin | forget everything cognee holds for this server |

Privacy-first: capture is **opt-in per channel**, and `/cognee-forget` removes
the server's memory on request.

## Setup

```bash
cd integrations/discord
pip install -e .

export DISCORD_BOT_TOKEN="your-bot-token"
export COGNEE_BASE_URL="http://localhost:8000"   # a running cognee server
export COGNEE_API_KEY="your-cognee-api-key"       # omit for a local server with auth disabled

python -m cognee_integration_discord
```

This variant talks to a **running cognee server** over HTTP, so cognee itself
(and its `LLM_API_KEY`) is configured on the server, not the bot.

Invite the bot with the `applications.commands` scope and the **Message Content**
privileged intent enabled (needed to read messages for capture).

## Running the bot from code

```python
from cognee_integration_discord import bot

bot.run()  # reads DISCORD_BOT_TOKEN + COGNEE_BASE_URL; talks to a cognee server
```

## Architecture

- `mapping.py` — dependency-free naming + citation helpers
- `adapter.py` — `ChatMemoryAdapter` seam + `CogneeHttpAdapter` (cognee HTTP API)
- `service.py` — platform-agnostic bot behavior (opt-in, ingest, answer, forget)
- `bot.py` — the only discord.py-facing module (slash commands + message listener)

The behavior in `service.py` is fully unit-tested against a fake adapter, so no
live bot or cognee instance is needed to run the tests:

```bash
pip install pytest
pytest
```

## Scope

v1 covers channel opt-in, live message capture, ask-with-citations, and forget.
Channel-history / pinned-message backfill and a dedicated FAQ knowledge-base mode
are planned follow-ups.
