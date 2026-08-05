# Cognee Spotlight for macOS

A Spotlight alternative powered by [cognee](https://github.com/topoteretes/cognee).
Press **⌥ Space** anywhere and search your files by name *and by meaning* — plus
ask questions and get answers straight out of your personal knowledge graph.

```
                 ┌───────────────────────────────────────────────┐
   ⌥ Space  ──▶  │  🔍  Search with cognee…                      │
                 ├───────────────────────────────────────────────┤
                 │  📄 quarterly-roadmap.md      (filename hit)  │
                 │  📄 pasta.txt                 [cognee]        │
                 │     "Carbonara: eggs, pecorino romano, …"     │
                 ├───────────────────────────────────────────────┤
                 │  ↩ Open  ⌘↩ Reveal  ⇧↩ Ask cognee  esc Close  │
                 └───────────────────────────────────────────────┘
```

## What's in the box

| Piece | What it is |
|---|---|
| `macos/` | Native SwiftUI/AppKit menu-bar app: global hotkey, floating non-activating search panel, settings window. Builds into a real `Cognee Spotlight.app` with Command Line Tools only — no Xcode. |
| `spotlight_backend/` | Small FastAPI server the app talks to. Walks your folders, feeds files into cognee, and merges instant filename matches with cognee's semantic results. |

The backend reaches cognee in one of three modes (`COGNEE_MODE`):

- **`local`** — cognee runs in-process on your machine (`pip install cognee`); needs `LLM_API_KEY`. Your files never leave the laptop except for LLM calls.
- **`cloud`** — talks to a cognee server or [cognee cloud](https://www.cognee.ai) over HTTP (`COGNEE_CLOUD_URL` + `COGNEE_CLOUD_API_KEY`).
- **`fake`** — no keys, no network: an in-memory substring index so you can try the whole app end to end in under a minute.

## Quick start (no API keys)

Requirements: macOS 13+, [uv](https://docs.astral.sh/uv/), Swift toolchain
(`xcode-select --install` is enough).

```bash
cd integrations/spotlight

# 1. start the backend in fake mode (default)
./scripts/run_backend.sh &

# 2. build and launch the app
./macos/scripts/make_app.sh
open "macos/dist/Cognee Spotlight.app"
```

Click the cognee icon in the menu bar → **Index a Folder…** and pick something
with notes in it (e.g. `~/Documents`). Then press **⌥ Space** and type.

- **↩** opens the selected file, **⌘↩** reveals it in Finder
- **⇧↩** asks cognee a question about your indexed content instead of listing files
- **esc** (or clicking elsewhere) dismisses the panel, just like Spotlight

## Onboarding (first launch)

On first launch — or any time via menu bar → **Setup…** — the app walks you
through choosing where your knowledge graph lives:

- **Cognee Cloud**: your tenant URL + API key. Indexing and search run on the
  tenant; the Mac only uploads files and reads results.
- **On this Mac**: an LLM API key (+ optional model). Everything stays local;
  the key is used to extract knowledge at indexing time and answer questions.

Plus an optional name/team for the handover features. **Save & Start** writes
`~/.cognee-spotlight/backend.env` (chmod 600, sourced by `run_backend.sh`
*after* the repo `.env`, so the app's choice wins) and starts the backend
itself, confirming health before it lets you go.

## Real semantic search

### Local cognee

```bash
cp .env.example .env
# in .env:  COGNEE_MODE=local  and  LLM_API_KEY=sk-...
./scripts/run_backend.sh &
```

Indexing now runs cognee's `add` + `cognify` pipeline (entity extraction,
knowledge-graph building), so first indexing takes a while and costs LLM
tokens. Search results marked `cognee` come from semantic chunk retrieval;
**⇧↩** answers come from `GRAPH_COMPLETION` over the graph.

### Cognee cloud / remote server

```bash
# in .env:
#   COGNEE_MODE=cloud
#   COGNEE_CLOUD_URL=https://api.cognee.ai   (or http://localhost:8000 for a local server)
#   COGNEE_CLOUD_API_KEY=...
./scripts/run_backend.sh &
```

Same app, same panel — files are uploaded to the server's `spotlight` dataset
and searched over HTTP.

## Testing

Backend (13 tests over the same HTTP surface the app uses, no keys needed):

```bash
uv sync
uv run pytest
```

App:

```bash
cd macos
swift build          # compile check
./scripts/make_app.sh   # full .app bundle, ad-hoc signed
```

Backend smoke test from the command line:

```bash
curl 'localhost:8765/health'
curl -X POST localhost:8765/index -H 'Content-Type: application/json' \
     -d '{"paths": ["/path/to/notes"]}'
curl 'localhost:8765/search?q=roadmap'
curl 'localhost:8765/search?q=what%20is%20our%20q3%20deadline&mode=answer'
```

## Team handover — pass learnings forward

Memory is organised in four layers, from shared to private (org → team → user
→ agent). The two inner layers are what you get out of the box: your private
index (user) and cognee's session memory (agent). The two outer layers come
alive when a **central cognee server** connects the team — the same server the
[cognee Claude Code plugin](../claude-code) uses, so learnings distilled from
agent sessions and learnings handed over by a senior are the same kind of
object in the same place:

```
handover-inbox-<user>    a learning for one person
team-<team>-memory       shared with one team
org-memory               shared with everyone
```

Configure identity + hub in `.env` (`SPOTLIGHT_USER`, `SPOTLIGHT_TEAM`,
`COGNEE_HUB_URL`, `COGNEE_HUB_API_KEY`) and restart the backend. Then:

- **Share**: menu bar → *Share a Learning…*, or press **⌘S** in the search
  panel to share the current answer / selected result. Address it to a
  username, `team:<name>`, or `org`.
- **Receive**: recipients get a macOS notification ("New learning from …"),
  the note lands in their *Inbox* (menu bar → *Inbox…*), and — the important
  part — it is **auto-ingested into their own searchable memory**, so ⌥ Space
  finds it like any of their own documents.

Senior-to-junior handover in practice: distill the runbook / gotcha / decision
once, `⌘S`, done — no hour-long walkthrough, and the knowledge is queryable
("what should I do before deploying?") instead of buried in a chat scrollback.

Try it on one machine (three terminals):

```bash
# 1. central server (single-principal demo posture)
ENABLE_BACKEND_ACCESS_CONTROL=false LLM_API_KEY=sk-... \
  uv run uvicorn cognee.api.client:app --port 8011

# 2. two identities
COGNEE_MODE=fake SPOTLIGHT_PORT=8767 SPOTLIGHT_USER=vasilije SPOTLIGHT_TEAM=core \
  SPOTLIGHT_DATA_DIR=/tmp/demo/senior COGNEE_HUB_URL=http://127.0.0.1:8011 \
  uv run python -m spotlight_backend
COGNEE_MODE=fake SPOTLIGHT_PORT=8766 SPOTLIGHT_USER=boris SPOTLIGHT_TEAM=core \
  SPOTLIGHT_DATA_DIR=/tmp/demo/junior COGNEE_HUB_URL=http://127.0.0.1:8011 \
  uv run python -m spotlight_backend

# 3. hand a learning over, then look at the junior's world
curl -X POST localhost:8767/share -H 'Content-Type: application/json' \
  -d '{"to":"boris","title":"Deploy runbook","body":"Run migrations first."}'
curl localhost:8766/inbox
curl 'localhost:8766/search?q=migrations'
```

On a real team, point every machine's `COGNEE_HUB_URL` at the shared server
(or cognee cloud); per-user API keys and dataset ACLs replace the demo
posture. The transport is one small class (`spotlight_backend/handover.py`)
built on the same `/api/v1/remember` + dataset routes the Claude Code plugin
uses.

## How search works

1. **Filename ranking** (instant, catalog-backed): exact > prefix > word-start
   > substring > subsequence — available the moment a folder is indexed.
2. **Semantic chunks** (cognee `CHUNKS` search): passages whose *meaning*
   matches the query, mapped back to openable files via the catalog and merged
   in, deduplicated by path.
3. **Answers** (cognee `GRAPH_COMPLETION`, on ⇧↩): an LLM answer grounded in
   the knowledge graph built from your files.

Indexing is incremental (mtime-based), skips hidden files, `node_modules`-type
directories, and files over 5 MB. State lives in `~/.cognee-spotlight/`.

## Layout

```
spotlight/
├── spotlight_backend/
│   ├── adapters.py    # local / cloud / fake cognee, one contract
│   ├── catalog.py     # indexed-file catalog + Spotlight-style name ranking
│   ├── indexer.py     # incremental folder walker
│   ├── server.py      # FastAPI: /health /index /index/status /search
│   └── config.py      # env-driven settings
├── tests/
├── macos/
│   ├── Sources/CogneeSpotlight/
│   │   ├── GlobalHotKey.swift          # ⌥Space via Carbon (no permissions needed)
│   │   ├── SearchPanelController.swift # borderless non-activating NSPanel
│   │   ├── SearchView.swift            # the panel UI (SwiftUI)
│   │   ├── SearchViewModel.swift       # debounced search, keyboard nav
│   │   ├── SettingsView.swift          # backend URL, folders, reindex
│   │   └── AppDelegate.swift           # menu-bar item, wiring
│   └── scripts/make_app.sh             # SPM build → Cognee Spotlight.app
└── scripts/run_backend.sh
```
