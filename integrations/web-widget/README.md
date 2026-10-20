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

Settings are read from the environment and from a `.env` file in the directory
the backend starts in; a variable set in the environment wins over `.env`.
`WIDGET_ENV_FILE` points at a different file, or turns `.env` loading off when
set to an empty value.

### How answers are recalled

Three settings control the recall each chat message runs against cognee:

| Variable | Default | Effect |
| -------- | ------- | ------ |
| `WIDGET_RECALL_SCOPE` | `graph,code` | Memory sources to recall from, comma-separated. `graph` is the docs answer; `code` adds results from the code graph of any repository ingested into the same dataset, returned next to the answer rather than in it; `session` adds the conversation's own history. |
| `WIDGET_SEARCH_TYPE` | `GRAPH_COMPLETION` | How the dataset is searched. Empty or `auto` lets cognee choose per question (usually `HYBRID_COMPLETION`). |
| `WIDGET_SYSTEM_PROMPT` | unset | The instruction the answering model gets. Unset, cognee's own default prompt is used ("Answer the question using the provided context. Be as brief as possible."). A value replaces that default; citations are unaffected. |
| `WIDGET_CODE_BASE_URL` | unset | Where the ingested repository can be browsed, e.g. `https://github.com/topoteretes/cognee/blob/main`. Set, related code links to its file and line. |

**Related code.** cognee's code graph only resolves exact names, so besides the
answer's own recall the backend picks up to three names out of the message
(class names, `Class.method`, dotted module paths from a log line, file paths
from a traceback) and looks each up with a code-only recall: no LLM, no session.
Whatever resolves comes back in the reply's `code` list and the widget shows it
under the answer as "Related code". A message that names no code makes no
lookup, and a failed lookup only leaves the list empty.

### Rate limits

`/api/chat` and `/api/forget` need no login, and every chat message runs a
cognee recall billed to your cognee account. Two guards keep that bounded:

| Variable | Default | Limits |
| -------- | ------- | ------ |
| `WIDGET_RATE_LIMIT_PER_IP_HOUR` | `20` | requests per visitor IP per hour (chat and forget together) |
| `WIDGET_RATE_LIMIT_PER_IP_DAY` | `100` | requests per visitor IP per day |
| `WIDGET_RATE_LIMIT_GLOBAL_HOUR` | `500` | chat requests per hour across all visitors |

`0` turns a limit off; any other value that is not a whole number stops the
backend at startup. A request over a limit gets `429` with a `Retry-After`
header and `{error, scope, message, retry_after}`, where `scope` is `visitor` or
`global`; the widget shows the message in the chat. The counters are kept in
the backend process, so run a single instance.

Behind a reverse proxy every request arrives from the proxy, so all visitors
would share one per-IP limit. Set `WIDGET_TRUSTED_PROXIES` to the proxy's IPs
or CIDR ranges (comma-separated), or `*` to believe whichever peer connects, and
the visitor's IP is read from `X-Forwarded-For`. Leave it unset when the backend
is reached directly: anyone can send that header.

### Contact form

When the assistant cannot answer reliably, it can hand the visitor over to your
team. Tell the answering model when to do it in `WIDGET_SYSTEM_PROMPT`, and to end
such a reply with the marker `[[contact_form]]`. The backend removes the marker
from the answer and the widget shows a short form under it: the visitor's email,
an optional note, and the chat transcript go to `POST /api/contact`, which emails
them to you with the visitor as `Reply-To`.

| Variable | Default | Effect |
| -------- | ------- | ------ |
| `WIDGET_CONTACT_TO` | unset | Address the requests are sent to. |
| `WIDGET_SMTP_HOST` | unset | SMTP server. With this or `WIDGET_CONTACT_TO` unset the form is off: no form is offered and `/api/contact` returns `404`. |
| `WIDGET_SMTP_PORT` | `587` | SMTP port. |
| `WIDGET_SMTP_USER` / `WIDGET_SMTP_PASSWORD` | unset | Login, if the server needs one. |
| `WIDGET_SMTP_FROM` | `WIDGET_SMTP_USER` | Sender address. |
| `WIDGET_SMTP_SECURITY` | `starttls` | `starttls`, `ssl` (implicit TLS, usually port 465) or `none`. |

`/api/contact` counts against the per-visitor [rate limits](#rate-limits), like
`/api/forget`. The transcript comes from the visitor's browser and is capped at its
last 60 messages.

### Seed your real docs

```python
import asyncio
from cognee_integration_web_widget import ChatMemoryAdapter

adapter = ChatMemoryAdapter()
asyncio.run(adapter.ingest_docs(site_id="acme", documents=["...your docs text..."]))
```

### Keep the docs in sync with GitHub

Point a GitHub push webhook at the backend and every push to the docs branch
updates the corpus: an added or edited page replaces the item ingested from the
same path, and a deleted page is deleted. Without this, re-ingesting an edited
page adds a second item next to the old one (cognee deduplicates by content, not
by path), and both keep answering.

1. Pick a random secret and set it as `WIDGET_GITHUB_WEBHOOK_SECRET`.
2. In the repository: **Settings → Webhooks → Add webhook**. Payload URL
   `https://<widget-host>/api/github/webhook`, content type `application/json`,
   the same secret, **Just the push event**.

| Variable | Default | Effect |
| -------- | ------- | ------ |
| `WIDGET_GITHUB_WEBHOOK_SECRET` | unset | Shared webhook secret. Unset, `/api/github/webhook` returns `404`. |
| `WIDGET_GITHUB_TOKEN` | unset | Token used to read changed files (`contents: read`). Required for a private repository; without one GitHub also allows only 60 reads an hour. |
| `WIDGET_GITHUB_DOCS_DIR` | repository root | Folder inside the repository the corpus mirrors. Paths are relative to it, like the folder a dashboard ingest picks. |
| `WIDGET_GITHUB_BRANCH` | the repository's default branch | Branch whose pushes are applied. |
| `WIDGET_GITHUB_REPO` | any | `owner/name` to accept pushes from; others are ignored. |
| `WIDGET_GITHUB_EXTENSIONS` | `.md,.mdx` | File types that are documentation. |
| `WIDGET_GITHUB_ROOT` | the docs folder's name | Node-set root for pages the corpus does not hold yet; existing pages keep theirs. |

Each changed file is read at the pushed commit and rendered as a dashboard ingest
renders it. A page whose content did not change is left alone. An edited page is
uploaded first and the old item deleted only once the upload is accepted, so a
refused upload leaves the old version answering rather than nothing. The webhook
answers `202` with the planned changes straight away and applies them in the
background; the outcome is in the server log and under `config.github_sync` in
`/api/dashboard`. Pages are matched to items by the path ingest records on them,
or by name for items ingested before paths were recorded.

## HTTP API

| Method | Path          | Body                                                                | Returns                          |
| ------ | ------------- | ------------------------------------------------------------------- | -------------------------------- |
| POST   | `/api/chat`   | `{message, conversation_id, visitor_id, site_id, opt_in, use_docs}` | `{answer, citations, code, session_id, contact_form}`, or `429` over a [rate limit](#rate-limits) |
| POST   | `/api/forget` | `{conversation_id, visitor_id, site_id}`                            | `{cleared, session_id}`, or `429` over a [rate limit](#rate-limits) |
| POST   | `/api/contact` | `{conversation_id, visitor_id, site_id, email, message, transcript, page_url}` | `{sent: true}`; `400` for a bad email, `502` if sending failed, `404` when the [contact form](#contact-form) is off |
| POST   | `/api/github/webhook` | a GitHub push event, signed with `X-Hub-Signature-256` | `202 {sha, upserts, removals}`; `200 {ignored}` for anything else it was sent; `401` for a bad signature, `404` when [GitHub sync](#keep-the-docs-in-sync-with-github) is off |
| GET    | `/`           | —                                                                   | demo "ask our docs" page         |
| GET    | `/widget.js`  | —                                                                   | the embeddable snippet           |

Each citation is `{document, snippet, data_id, chunk_id}`. Each entry of `code` is
`{name, symbol, kind, file, line, url}`: the short name shown, the full code-graph
symbol, its node type (`CodeSymbol`, `CodeFileReference`, ...), where it is defined,
and a link when `WIDGET_CODE_BASE_URL` is set.

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
