# cognee-integration-web-widget

An embeddable chat widget — one `<script>` tag — that answers questions from
your docs/site with **cited** sources, scopes each visitor's conversation with
its own `session_id`, and lets anyone **forget** their chat or opt out entirely.

## Overview

The backend is a thin, CORS-enabled FastAPI proxy that talks to a **running
cognee server** over HTTP (`POST /api/v1/remember | recall | forget`). It needs
no in-process cognee and no LLM key of its own — those live on the server. All
memory logic sits behind a framework-agnostic `ChatMemoryAdapter`, so the same
core could back a WhatsApp or MS Teams bot without touching the transport.

## How memory is scoped

Each browser conversation maps to a stable cognee `session_id`:

```
web:{site_id}:{visitor_id}:{conversation_id}
```

- **Default boundary: per visitor-conversation.** Recalling with that
  `session_id` lets cognee's session-aware recall use *and persist* the
  conversation's own history, so one visitor's chat never leaks into another's.
- **"Ask our docs" mode:** your docs are seeded once into a shared, read-only
  dataset `web:{site_id}:docs`. Every conversation can `recall` from it; none
  writes to it. Clean split between shared knowledge and personal memory.
- **Citations:** recall runs with `include_references=true`, so cognee appends a
  grounded `Evidence:` block to the answer. The adapter parses that block into
  inline citations and strips it from the displayed prose; an answer with no
  evidence (e.g. a plain "I don't have that yet") is simply shown uncited.
- **Forget / opt-out:** the `/forget` command (and the widget's *Forget me*
  link) best-effort clears the conversation; unticking *Remember this chat*
  answers statelessly, so no `session_id` is passed and nothing is persisted.

## Install

```bash
cd integrations/web-widget
uv sync              # or: pip install -e .
```

## Run in 5 minutes

```bash
# 1. Start a cognee server somewhere and point the widget at it.
cp .env.example .env      # then edit COGNEE_BASE_URL / COGNEE_API_KEY

# 2. Start the widget backend (seeds a small demo docs corpus on boot).
cognee-web-widget         # or: python -m cognee_integration_web_widget.server

# 3. Open the "ask our docs" demo.
open http://127.0.0.1:8000
```

Ask *"What is cognee?"*, then type `/forget` to clear the conversation.

### Embed on your own site

```html
<script src="http://127.0.0.1:8000/widget.js"
        data-site-id="acme"
        data-api="http://127.0.0.1:8000"></script>
```

The widget is embedded cross-origin, so the backend enables CORS for `/api/*`
(open by default; set `WIDGET_ALLOWED_ORIGINS="https://your-site.com"` to
restrict it in production). The backend binds `127.0.0.1` by default — set
`WEB_HOST` / `WEB_PORT` to change it.

### Seed your real docs

```python
import asyncio
from cognee_integration_web_widget import ChatMemoryAdapter

adapter = ChatMemoryAdapter()
asyncio.run(adapter.ingest_docs(site_id="acme", documents=["...your docs text..."]))
```

## HTTP API

| Method | Path          | Body                                                                | Returns                          |
| ------ | ------------- | ------------------------------------------------------------------- | -------------------------------- |
| POST   | `/api/chat`   | `{message, conversation_id, visitor_id, site_id, opt_in, use_docs}` | `{answer, citations, session_id}`|
| POST   | `/api/forget` | `{conversation_id, visitor_id, site_id}`                            | `{cleared, session_id}`          |
| GET    | `/`           | —                                                                   | demo "ask our docs" page         |
| GET    | `/widget.js`  | —                                                                   | the embeddable snippet           |

Each citation is `{document, snippet, data_id, chunk_id}`.

## What changed from the in-process example

This package is the HTTP port of cognee's `examples/bots/web_widget`. The
original reached into cognee internals; those are not reachable over HTTP, so:

- **Session persistence** is now achieved purely by passing `session_id` to
  `/recall` (the server's session-aware recall does the persisting) instead of
  the in-process `get_session_manager`.
- **Forget** clears a **dataset** via `POST /api/v1/forget` (the only forget the
  HTTP surface exposes). The server has no `DELETE /sessions` endpoint, so the
  session cache itself is not independently deletable over HTTP — `/forget` is
  therefore best-effort at the conversation level. `everything` is always
  `false`, so a visitor's forget can never wipe another dataset.
- **Missing docs corpus** degrades gracefully: a never-seeded dataset returns a
  4xx that the HTTP client maps to no results, so the widget shows an empty
  answer instead of erroring (the server seeds the demo corpus on boot).

## Testing

Fast and keyless — the adapter runs against a fake HTTP client and the real
client is exercised against a stub transport, so no cognee server or LLM keys
are needed:

```bash
uv run pytest tests/ -q
```
