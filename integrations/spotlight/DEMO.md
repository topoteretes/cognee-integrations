# Client demo: one memory for people, files, and coding agents

**The pitch in one line:** every document, every AI-coding session, and every
teammate's hard-won lesson lands in one knowledge graph — searchable from a
Spotlight-style panel, shareable person-to-person, and remembered by your
coding agents.

**Cast:** you (vasilije, the senior) and boris (the junior teammate) — two
profiles of the same app on this Mac, both connected to your cognee cloud
tenant. In production each has their own API key and dataset ACLs; the demo
runs both on one key.

## Before the client arrives (5 min)

```bash
cd integrations/spotlight && ./scripts/demo_prep.sh
```

It starts both backends + the app and prints a ✅/❌ checklist. Then:

1. Menu bar (cognee icon) → **Profile** → confirm `default` is checked.
2. ⌥ Space → run **two** semantic searches (`where do competitors threaten us`,
   wait for results, then another) → esc. The first query after a backend
   start is cold (~12 s and may come back empty); the second is ~4 s. Never
   let the client's first query be the cold one.
3. Menu bar → **Knowledge Graph…** — leave the tab open in the browser; it
   takes ~10 s to build and you'll want it instant during Beat 2½.
4. Open **Inbox…** on both profiles and mark everything read (click each item),
   so the demo's share triggers a fresh notification.

**Timing note:** do Beat 4's ⌘S share *before* a natural pause (the tenant
cognifies the note in the background for ~30–60 s before it's semantically
searchable on boris's side; the inbox + notification are immediate).

---

## Beat 1 — "Spotlight, but it understands meaning" (2 min)

⌥ Space, type slowly: `meridian` — filename matches appear **instantly**.

Now the money query: **`where do our competitors threaten us most`** — pause,
watch the graph-pulse animation, and semantic results land:
`meridian_competitor_landscape.md` with the threat-tier snippet. No
keyword overlap with the filename — that's meaning, not string matching.

- ↩ opens the file. Point out the purple spark = "found by meaning".
- Talking point: files were indexed once from the app (menu → Index a
  Folder…); indexing and search run on the cloud tenant.

## Beat 2 — Ask your knowledge, get an answer (1 min)

⌥ Space → **`who are Meridian's main competitors and where are we exposed?`** → **⇧↩**.

The serif-set answer comes from GRAPH_COMPLETION over the tenant's knowledge
graph — with the "FROM YOUR KNOWLEDGE GRAPH" eyebrow. ~5–8 s; narrate while
it thinks ("it's walking the graph, not grepping").

## Beat 2½ — Show them the brain (1 min)

Menu bar → **Knowledge Graph…** (pre-opened tab). This is **cognee's own
product visualization** of the tenant graph: Documents → Chunks → Entities →
Types → Summaries as columns, with the Graph / Schema / Memory / Semantic
tabs on top and Story / Flow / Force layouts at the bottom.

- Hit **Force** for the organic hairball moment, **Story** to walk the
  document→entity pipeline; the node search box jumps to any entity.
- The **memory layer** switcher (top right) flips between datasets: `spotlight`
  (your files), `agent_sessions` (what the coding agents did), and the
  `handover-inbox-…` datasets (team learnings).

Talking point: *"the answers you just saw are walks over this graph — this is
the company's memory, not a search index."* This is usually the moment the
room leans in.

## Beat 3 — Coding agents write memory too (2 min)

Talking point first: "My Claude Code and Codex sessions automatically distill
what they did into the same tenant — the `agent_sessions` dataset. Nobody
wrote this documentation; the sessions left it behind."

⌥ Space → **`what did we change to make search fast`** → ⇧↩.
Expected: an answer citing the access-control/worker findings from the actual
engineering session that built this app. **Leave the answer on screen** — it
is the handover payload for the next beat.

Visual payoff: open
`http://127.0.0.1:8765/graph?dataset=agent_sessions&query=search performance fix`
— the graph zooms to the neighborhood of that engineering work, entities and
session chunks around the exact fix the answer just described. (The agent
layer holds thousands of nodes; the `query` parameter is what makes it
navigable — an unfocused view is a 350-node sample.)

Optional flex: open a terminal with Claude Code and ask *"what did we ship in
the spotlight integration?"* — the plugin recalls the same tenant memory.
Status line shows `cognee: agent_sessions · cloud ✓`.

## Beat 4 — Hand the session learning to a teammate (3 min)

The line that sells it: *"My agent learned this while coding. Watch me hand
that learning to my junior in ten seconds — no meeting, no wiki page."*

1. With beat 3's answer still on screen, press **⌘S** — a recipient row
   appears right in the panel: `boris · alex · priya · team:core · org`.
   Arrow to **boris** (highlighted by default), press **↩** — a "Shared with
   boris ✓" toast confirms. No window, no typing. (**⌘⇧S** still opens the
   full sheet when you want to edit the text.)
2. Menu bar → **Profile → boris**. You are now the junior: separate identity,
   separate inbox, same tenant.
3. Within ~45 s boris gets the macOS notification — **"New learning from
   vasilije"**. Open **Inbox…**: unread dot, purple `inbox` badge, and the
   full learning — question, answer, provenance.
4. The kicker: ⌥ Space (still as boris) → **`why is spotlight search fast
   now`** → the learning answers as *boris's own memory*. The knowledge went
   agent session → senior's answer → junior's memory without anyone writing
   a document.

Talking point: addressed sharing (`boris`), team broadcast (`team:core`), or
org-wide (`org`) — backed by per-dataset ACLs on the tenant (read / write /
share grants per user or role). Onboarding a new engineer = granting them the
team's datasets; their first day starts with the team's memory, including
what the agents learned.

## Beat 5 — Close the loop (30 s)

Switch Profile back to `default`. "One graph: my files, my agents' work, my
team's lessons. Spotlight is just the window into it."

## Optional flex — memory that argues back (1 min)

Needs `SPOTLIGHT_EXPERIMENTS=1` in `~/.cognee-spotlight/backend.env`
(restart the backend after). Three things switch on:

1. **Conflicting memory.** ⌥ Space → **`what happened with the websockets
   version`** → ⇧↩. Under the answer an orange ⚠ chip appears:
   *"conflicting memory: websockets version conflict vs targeted code
   inspection"* — a real disagreement cognee recorded from an agent session
   instead of silently overwriting one side. Talking point: *"memory that
   never loses the losing argument."*
2. **Temporal phrasing.** Ask **`what changed in search since last month`**
   — time-cued questions route to temporal search where the deployment
   supports it, and degrade to a normal graph answer where it doesn't
   (cognee cloud today). Same panel, no error either way.
3. **Feedback thumbs.** Every answer gains 👍/👎. A 👍 re-ingests the Q&A as
   a confirmed learning — the graph gets more sure of itself; a 👎 is logged
   for correction.

Leave the flag off if you want the demo minimal — everything above is
additive and the rest of the script is unchanged.

---

## If something misbehaves

| Symptom | Fix |
|---|---|
| Panel shows nothing | `./scripts/demo_prep.sh` again (backend down is the usual cause); filename search works even if the tenant is slow |
| Semantic results slow/empty | The 12 s budget protects the panel — retype the query once; tenant cold starts recover on the second try |
| No notification on boris | It polls every 45 s — open Inbox… directly instead; the item is there |
| ⇧↩ answer weak | Re-ask with more words; GRAPH_COMPLETION rewards specific questions |
| Total meltdown | `SPOTLIGHT_DEBUG_QUERY=meridian "macos/dist/Cognee Spotlight.app/Contents/MacOS/CogneeSpotlight"` auto-opens the panel with results for a screenshot-grade fallback |

## Reset between rehearsals

Mark inboxes read (open Inbox…, click items) and re-run `demo_prep.sh`.
Shares accumulate in `handover-inbox-boris` on the tenant; that's fine — the
inbox shows newest first.
