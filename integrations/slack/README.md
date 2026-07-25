# cognee-integration-slack

A Slack bot that gives a workspace **per-channel memory** backed by cognee. It
ingests messages from opted-in channels into that channel's own knowledge graph
and answers `@cognee …` mentions / `/recall` with **cited** answers linking back
to the source Slack messages.

## Overview

The bot talks to a **running cognee server** over HTTP (`POST /api/v1/add`,
`/cognify`, `/search`, `/forget`) — no in-process cognee and no LLM key of its
own (those live on the server). All memory logic sits behind a thin `ChatMemory`
adapter, and Slack I/O runs over Socket Mode (no public URL needed).

## Features

- **Per-channel memory isolation** — each channel is one cognee dataset
  (`slack_<channel_id>`), the boundary `/cognee-forget` clears.
- **Batched ingestion** — messages are added cheaply and the graph is built
  (cognify) once per batch, keeping cost sane; a question always flushes pending
  messages first.
- **Cited answers** — a CHUNKS search filtered to the channel's node set recovers
  each source message's permalink/author (carried as a provenance header inside
  the stored text) and renders them as Slack Block Kit source links.
- **Opt-in per channel** (`/cognee-optin` / `/cognee-optout`), with a disclosure
  posted on first opt-in.

## Commands

| Trigger | Effect |
|---|---|
| `@cognee <question>` | answer from the channel's memory, with sources |
| `/recall <question>` | same, as a slash command |
| `/cognee-optin` / `/cognee-optout` | start / stop capturing this channel |
| `/cognee-forget` | delete this channel's memory (dataset-level) |

## Installation

```bash
cd integrations/slack
uv sync              # or: pip install -e .
```

## Setup

1. Create a Slack app with Socket Mode enabled; grab the bot token (`xoxb-…`)
   and app token (`xapp-…`). Subscribe to `message.channels` and `app_mention`
   events and add the `/recall`, `/cognee-optin`, `/cognee-optout`,
   `/cognee-forget` slash commands.
2. Point the bot at a running cognee server.

```bash
cp .env.example .env      # then edit
export SLACK_BOT_TOKEN="xoxb-..." SLACK_APP_TOKEN="xapp-..."
export COGNEE_BASE_URL="http://localhost:8000"   # a running cognee server
export COGNEE_API_KEY="..."                        # omit for a local server with auth off

python -m cognee_integration_slack
```

## Notes & limitations

- **Channel-level forget only.** cognee forgets at the dataset (channel) level;
  a per-user "forget me" is a follow-up.
- **Citations are a parallel CHUNKS search** decoupled from the prose answer;
  a message whose permalink is unknown degrades to a plain-text source, never a
  broken link. Edited/deleted Slack messages are not re-synced.
- Public channels only (subscribe `message.channels`).

## Testing

Fast and keyless — the adapter runs against a fake HTTP client, the Slack
handlers against mocks, and the HTTP client against a stub transport; no Slack,
cognee, or LLM keys are needed:

```bash
uv run pytest tests/ -v
```
