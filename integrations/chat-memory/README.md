# cognee-integration-chat-memory

The shared **chat-memory adapter core** for cognee-powered bots. Every cognee
chat bot (Slack, Telegram, Discord, an embeddable web widget, a personal
second brain) plugs into this one small, framework-agnostic layer, so each bot
stays thin and they all share one memory model built on cognee's
`remember` / `recall` / `forget` primitives.

## Overview

A bot supplies just two things:

1. a **scope strategy** — the memory boundary it uses (per channel, per user, …), and
2. a translation of platform events into a `Conversation` + `Message`.

Everything else — consent gating, the provenance stamp, citation assembly, and
the forget paths — lives in `ChatMemoryAdapter` and is identical across
platforms.

## Features

- **Three primitives**: `ingest` (remember, background), `answer` (recall with
  citations), `forget` (whole-scope or per-user "forget me").
- **Two scope strategies**: `per_channel_scope` (shared team memory) and
  `per_user_scope` (a cross-transport personal brain) — or write your own
  one-line `Conversation -> Scope`.
- **Consent-first**: a channel bot stays silent for a user until they opt in;
  DMs opt in by use. Pluggable `ConsentStore`.
- **Citations**: recalled answers carry `Citation`s back to the source message
  (permalink + author), resolved through the provenance the adapter stamps at
  ingest.
- **Pluggable backends** behind one four-method contract (see below).

## Installation

```bash
cd integrations/chat-memory
uv sync            # or: pip install -e .
```

`httpx` is the only runtime dependency. For the in-process cognee SDK backend,
install the extra: `pip install -e ".[sdk]"`.

## Quick start

```python
from cognee_integration_chat_memory import (
    ChatMemoryAdapter, Conversation, Message, per_channel_scope,
)

# Default backend talks to a running cognee server (COGNEE_BASE_URL / COGNEE_API_KEY).
adapter = ChatMemoryAdapter(scope=per_channel_scope)

convo = Conversation(platform="slack", workspace="T1", channel="C1", user="U1")
adapter.set_consent("U1", True)
await adapter.ingest(convo, Message(text="We ship on Friday.", user="U1",
                                    permalink="https://slack/archives/C1/p1"))
answer = await adapter.answer(convo, "when do we ship?")
print(answer.text)
for c in answer.citations:
    print(c.user, c.permalink)
```

## Memory model

| Concept | Maps to | Why |
|---|---|---|
| Scope `dataset` | cognee dataset | the durable graph + recall boundary, and what `forget` wipes |
| Scope `session` | cognee session id | the live-conversation recency axis |
| Message | remembered document + provenance | citations link answers back to the source |

`dataset` and `session` are independent so a per-user brain can have one durable
`dataset` (recallable across every transport) with a per-transport `session`.

## Backends

All satisfy one `MemoryBackend` contract (`remember` / `recall` / `forget_scope`
/ `forget_user`):

- **`CogneeHttpMemoryBackend`** (default) — talks to a running cognee server over
  `POST /api/v1/remember | recall | forget`. No in-process cognee; cognee's
  `LLM_API_KEY` lives on the server. Provenance rides inside the stored text and
  is parsed back from recalled snippets, so citations resolve without database
  access.
- **`InMemoryMemoryBackend`** — dependency-free, keyless; ranks recall by keyword
  overlap. For local dev, demos, and the test suite.
- **`CogneeMemoryBackend`** — in-process cognee Python SDK (needs the `[sdk]`
  extra). Resolves citations via cognee's `data_id` provenance and supports
  precise per-user forget inside a shared dataset.

> **Per-user forget over HTTP:** cognee's HTTP surface exposes only whole-dataset
> forget, so `CogneeHttpMemoryBackend.forget_user` wipes the dataset. Pair
> per-user "forget me" with `per_user_scope` (a dataset per user) so it removes
> only that user's brain; precise per-user forget in a *shared* dataset needs the
> SDK backend.

## Run the demo

```bash
python examples/console_bot.py                                   # keyless, in-memory
COGNEE_BASE_URL=http://localhost:8000 python examples/console_bot.py   # real cognee over HTTP
```

## Testing

Fast and keyless — the real adapter runs against `InMemoryMemoryBackend` and the
HTTP backend is exercised against a stub transport, so no LLM, database, or keys
are needed:

```bash
uv run pytest tests/ -v
```
