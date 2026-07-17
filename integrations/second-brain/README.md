# cognee-integration-second-brain

**One memory graph, reachable from many front-ends.** Capture a note in
Telegram, recall it from the web, and both resolve to the same private brain
because the bot knows they are the same person. Capture anywhere, recall
anywhere, forget everywhere.

## Overview

The bot talks to a **running cognee server** over HTTP
(`POST /api/v1/remember | recall | forget`) — it needs no in-process cognee and
no LLM key of its own (those live on the server). All memory logic sits behind a
thin `ChatMemoryAdapter`, and the piece this bot adds on top is a
**cross-transport identity layer** that maps many external identities onto one
canonical user and one `brain:{user}` dataset.

## Features

- **Cross-transport memory** — a note saved in Telegram is recalled from the
  web (and vice versa), because both identities resolve to one `brain:{user}`
  dataset.
- **Identity linking** — `/link` on one app issues a short code; entering it on
  another merges the two identities onto one brain.
- **Cited recall** — answers link back to the exact source message (transport +
  date + deeplink). A "no information" answer is **never** cited.
- **`/forget me`** — wipes the whole brain across every connected app and drops
  the identity links.
- **Opt-out** per user (`/optout` / `/optin`); capture is on by default.

## Slash commands

| Command | Effect |
|---|---|
| _(any note)_ | remember it |
| _(a question ending `?`)_ | recall it, with sources |
| `/link` | issue a code to connect another app to this brain |
| `/link <code>` | enter a code from your other app to share one brain |
| `/forget me` | wipe your whole brain across every app |
| `/optout` / `/optin` | pause / resume capturing new notes |
| `/help` | show the command list |

`/note <text>` forces capture of a question-shaped note; `/ask <q>` or
`/recall <q>` force recall without a `?`.

## Installation

```bash
cd integrations/second-brain
uv sync              # or: pip install -e .
```

## Quick start (no cognee server, no key)

The fake in-memory adapter proves the whole flow — identity, routing, citations,
forget — with substring recall and zero setup:

```bash
USE_FAKE_ADAPTER=true python -m cognee_integration_second_brain
```

The web transport comes up on `http://127.0.0.1:8080/message` (loopback by
default; set `WEB_HOST=0.0.0.0` to expose it, e.g. in a container).

```bash
# Save a note as user "alice"
curl -s localhost:8080/message -H 'content-type: application/json' \
  -d '{"user": "alice", "text": "I parked the car on level 3 of the garage"}'
# {"reply":"Saved to your brain."}

# Ask for it back (end with "?" to recall)
curl -s localhost:8080/message -H 'content-type: application/json' \
  -d '{"user": "alice", "text": "where did I park?"}'
```

## Real cognee-backed memory

Point the bot at a running cognee server for real semantic recall across
sessions and transports:

```bash
cp .env.example .env      # then edit
export COGNEE_BASE_URL="http://localhost:8000"   # a running cognee server
export COGNEE_API_KEY="..."                        # omit for a local server with auth off
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."      # optional; enables Telegram too

python -m cognee_integration_second_brain
```

The bot stores notes in a per-user cognee dataset (`brain:{user}`) and recalls
them with graph completion (`search_type=GRAPH_COMPLETION`,
`include_references=true`), so cross-source questions run as a real multi-hop
traversal. Ingest is dataset-only (no session cache), so recall targets the
whole brain — the persistence-across-sessions story this bot demonstrates.

## Add a second transport (Telegram)

Create a bot with [@BotFather](https://t.me/BotFather), set `TELEGRAM_BOT_TOKEN`,
and restart. The web transport always runs; Telegram runs too when a token is
present. Then link the two front-ends:

1. In Telegram, send `/link`. The bot replies with a short code.
2. Hit the web endpoint with `/link <that code>` as the text.

From then on a note saved in Telegram is recalled from the web and back. Send
`/forget me` from either side to wipe the whole brain everywhere.

## How it works

```
http_client.py         CogneeHttpClient — talks to a cognee server over HTTP. No cognee import.
interface.py           the ChatMemoryAdapter contract + Conversation/Message/Answer/Citation.
fake_adapter.py        in-memory adapter, for tests and the no-key run.
cognee_adapter.py      the real adapter, over CogneeHttpClient. Holds the citation guard.
identity.py            link table (external identity -> canonical user) + one-time-code linking.
consent.py             per-user opt-in / opt-out.
commands.py            /link, /forget, /optin, /optout, /help.
router.py              resolve identity, route capture vs recall, render replies.
telegram_transport.py  raw long-polling over the Telegram Bot API via httpx.
web_transport.py       a single FastAPI POST /message endpoint.
```

**Memory boundary** — the dataset is keyed by the canonical user (`brain:{user}`),
so a note from any transport lands in one shared brain.

**Citations** — the adapter records a source-to-message map at ingest and
resolves citations from cognee's grounded `Evidence:` block. A note is cited only
when it was actually retrieved **and** the answer uses a distinctive term from it
that is not already in the query — so a refusal, which only echoes the query,
cites nothing however it is phrased.

**Forget** — `/forget me` is whole-brain: one `forget(brain:{user})` wipes
everything across every transport, and the identity links are then dropped so no
app re-attaches.

## Testing

Fast and keyless — the fake adapter drives the router/identity/forget tests, the
real adapter runs against a fake HTTP client, and the client itself is exercised
against a stub transport. No cognee, network, or Telegram/LLM keys:

```bash
uv run pytest tests/ -v
```
