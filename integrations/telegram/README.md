# cognee-integration-telegram

A Telegram bot where **each chat is a cognee memory**. It quietly captures what's
shared in a chat into that chat's own knowledge graph, answers `/ask` questions
from it with **tappable sources** back to the original messages, and can `/forget`
everything on request.

## Overview

The bot talks to a **running cognee server** over HTTP
(`POST /api/v1/remember | recall | forget`) — it needs no in-process cognee and
no LLM key of its own (those live on the server). All memory logic sits behind a
thin `CogneeMemoryAdapter`, so the same core could back a Slack/Discord bot.

## Features

- **Per-chat memory isolation** — DMs, groups, and forum topics each map to their
  own cognee dataset (`telegram_dm_<user>` / `telegram_group_<chat>[_<thread>]`).
- **Cited recall** — answers link back to the exact source message (`t.me/c/…`
  deep links in supergroups; quoted snippets elsewhere). A "no information"
  answer is never cited.
- **Opt-out** per chat (`/optout` / `/optin`); capture is on by default.
- **Passive capture** of text and media captions (forwarded articles, links).

## Slash commands

| Command | Effect |
|---|---|
| `/ask <question>` | answer from this chat's memory, with sources |
| `/forget` | wipe this chat's memory (graph + vectors) |
| `/optout` | stop capturing here (`/optin` to resume) |
| `/start` / `/help` | intro |

## Installation

```bash
cd integrations/telegram
uv sync              # or: pip install -e .
```

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Point the bot at a running cognee server.

```bash
cp .env.example .env      # then edit
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
export COGNEE_BASE_URL="http://localhost:8000"   # a running cognee server
export COGNEE_API_KEY="..."                        # omit for a local server with auth off

python -m cognee_integration_telegram              # long polling; no webhook/URL needed
```

For group privacy: either disable BotFather's group privacy mode, or add the bot
as an admin, so it can read messages to capture them.

## Memory model

| Telegram | cognee dataset |
|---|---|
| DM (private) | `telegram_dm_<user_id>` |
| group / supergroup | `telegram_group_<chat_id>` |
| forum topic | `telegram_group_<chat_id>_<thread>` |

Citations are resolved by an in-memory ledger that maps recalled evidence back to
the ingested message (bounded to the most recent 1000 per chat).

## Testing

Fast and keyless — the adapter runs against a fake HTTP client and the real
client is exercised against a stub transport, so no Telegram, cognee, or LLM
keys are needed:

```bash
uv run pytest tests/ -v
```
