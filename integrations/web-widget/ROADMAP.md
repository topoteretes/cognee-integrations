# cognee web-widget: roadmap

Living document; add to it as we go. The status for each item is one of
**done**, **next**, **planned**, **TBD** (needs a decision) or **blocked** (waiting
on someone else).

Related: [`DEPLOYMENT-REQUIREMENTS.md`](DEPLOYMENT-REQUIREMENTS.md) (hosting),
[`COGNEE-FINDINGS.md`](COGNEE-FINDINGS.md) (what cognee should change).

---

## 0. Go-live prerequisites

Not a theme of its own, but everything else assumes the backend runs somewhere
permanent.

| Item | Status | Notes |
|---|---|---|
| Dedicated server with a fixed HTTPS hostname | blocked | Requested from the cloud team, see `DEPLOYMENT-REQUIREMENTS.md`. Ends the quick-tunnel cycle, where every restart means editing `widget.js`. |
| Stop seeding demo docs on every boot | next | `lifespan` in `server.py` writes sample paragraphs into the live `web:demo:docs` dataset on each start. |
| Tolerate slow dataset deletes | next | Drop the graph cache whatever the outcome, give the delete a longer timeout, and report "still deleting" instead of an error. See finding #12. |
| Point `widget.js` at the new hostname | blocked | Waiting on the hostname. |
| `/health` route | planned | If the cloud team's tooling needs it. |

---

## 1. Log-in capability

**Goal:** operators sign in to the dashboard with a proper log-in instead of the
shared token (`WIDGET_DASHBOARD_TOKEN`). Visitors using the chat stay anonymous.

| Item | Status | Notes |
|---|---|---|
| Log-in for the dashboard, replacing the token | TBD | How it works (provider, where the log-in happens, who gets access) is to be worked out later. |

---

## 2. Better distillation

**Goal:** conversations reliably turn into useful lessons, and those lessons tell
us which parts of the documentation need improving. Visitors' pain points (what
they asked, what the docs failed to answer) become a ranked list of docs work.

Where it is today: conversations live only in cognee's session memory (the
backend writes no transcript of its own). Every turn shows as **pending** in the
dashboard, with a countdown to when cognee forgets the session, until an
operator presses **Distil**. For each conversation with pending turns, the
backend calls `POST /api/v1/improve`, which moves the session into permanent
memory (`web:demo:conversations`) and distils lessons from it. When the run
finishes, the turns asked before it started show as **synced**.

| Item | Status | Notes |
|---|---|---|
| Manual trigger from the dashboard | done | Replaced the page-close signal, which couldn't tell a closed tab from a reload. |
| Only real conversations are distilled | done | Widget sessions with at least one question and pending turns, not every session cognee holds. |
| Synced / pending status per turn, with countdown | done | Kept in `WIDGET_SYNC_STATE_PATH`. The countdown is an estimate (7 days after last activity); the tenant doesn't report its real setting. |
| No transcript of our own | done | Removed: it duplicated what cognee's session already holds, cost a write per answer, and lost turns when a write failed. |
| **Decide what lessons are for** | done | First use: **docs gaps for the docs team.** Visitors' pain points show which parts of the documentation need work. Feeding lessons into answers is still open. Lessons land in `web:demo:conversations` and answers are recalled only from `web:demo:docs` (`adapter.py`), so lessons still never reach a visitor. |
| Trace lessons to docs pages | next | A lesson can be traced to its conversation (`session_learnings:<session id>`) but not to a page. The session keeps each turn's `used_graph_element_ids`; map those to the cited pages. A lesson whose conversation cited nothing points to a missing page. |
| Docs gaps view in the dashboard | planned | Lessons grouped by the page they point to, ranked by how many conversations hit it, with a "no page covers this" bucket. Each row shows the lesson and links to the page and the conversation. |
| Unanswered questions as a second signal | planned | The analytics already count questions the widget could not answer, exactly: the adapter returns one fixed "no answer" string (`server.py`). List them in the docs gaps view next to the lessons, grouped by topic. They need no distillation, so they show gaps from day one. |
| Hand docs gaps to the docs team | TBD | Export as Markdown, open Linear issues, or feed the docs-edit automation in `tools/`. Decide once the view shows real gaps. |
| Automatic distillation | planned | Distil a conversation after N minutes with no new turns, as a background sweep. cognee runs nothing on a schedule, and a session that expires before anyone distils it is lost. |
| Show what each run produced | blocked | cognee's `/improve` returns a pipeline status, not per-stage results (finding #6). For now we can only see lessons appear afterwards. |
| Warn before a session expires | planned | Pending conversations already show a countdown. Add a notice when one is close (for example under 24 h), since an expired session is gone with nothing left to distil. |
| Review lessons in the dashboard | planned | Read, approve or delete a lesson, especially a wrong one. |
| Avoid `persist_session_qa` side effects | blocked | Every improve run also copies the session into the graph, and that stage can lose turns (finding #1). Needs cognee to let HTTP callers switch stages off. |
| Distillation status survives a restart | planned | Today it's held in memory only (`_distil_run`). |

---

## 3. Better UI

**Goal:** the dashboard shows what is going on without anyone reading logs, and the
widget feels native on the docs site.

| Item | Status | Notes |
|---|---|---|
| Refresh button for the open corpus tab | done | Files, repositories or conversation memory. Repositories skip the 15-minute graph cache. |
| Ingest progress that survives closing the tab | planned | The upload needs the tab open; after that, progress is only visible by refreshing. Show per-ingest status: files accepted, processed and failed. |
| Auto-refresh while an ingest or distillation runs | planned | Poll the open tab until the pipeline settles, instead of pressing Refresh. |
| Per-file results after an ingest | planned | Show which files were refused inside an otherwise successful batch. Today they're only in the page's upload summary. |
| Clear shows "still deleting" instead of an error | next | Tied to the slow-delete fix in section 0. |
| Render markdown in chat answers | planned | Answers are markdown (code blocks, inline code, bold, lists, links, tables), but `widget.js` inserts them as plain text (`textContent` in `el()` / `addMsg()`), so visitors see raw code fences and asterisks. Parse and render it, **sanitised**: answers are LLM output, so writing them into the page as HTML without escaping would allow script injection. The widget has no build step and Mintlify inlines it, so the parser has to be small and self-contained (or a vetted library inlined with a sanitiser). The dashboard's conversation view shows the same raw markdown and should use the same renderer. |
| Widget look and behaviour on the Mintlify site | TBD | Needs a review pass on the preview (`cognee-cognee-widget-integration.mintlify.site`): theme, mobile, accessibility. |

---

## 4. Answers: custom prompt

**Goal:** we control how the widget answers: tone, scope, format, and what it says
when the docs don't cover something.

| Item | Status | Notes |
|---|---|---|
| Custom system prompt for answers | planned | `POST /api/v1/recall` already takes `system_prompt`. The widget sends none, so answers use cognee's default ("Answer the question using the provided context. Be as brief as possible."). Add `WIDGET_SYSTEM_PROMPT` (or a prompt file) and pass it through `CogneeHttpClient.recall`. About 30 min. Citations are unaffected: cognee adds the Evidence block after the answer, from the retrieved chunks. |
| Edit the prompt from the dashboard | planned | Needs somewhere durable to keep it (the backend has no storage), or keep it in config and redeploy to change it. |
| Choice of LLM model | TBD | On cognee Cloud the tenant manages the model (sessions report `litellm_proxy/litellm`), and recall has no per-request model parameter. Self-hosted cognee can switch provider and model with `POST /settings`, but that changes the whole instance. Question for the cognee Cloud team. |

---

## 5. Works with self-hosted and Cloud cognee

**Goal:** the same widget backend runs against a self-hosted cognee server or a
cognee Cloud tenant, chosen only by configuration (`COGNEE_BASE_URL`, and
`COGNEE_API_KEY` when there is one).

Today it has only been used against the Cloud tenant. The two differ in ways we
have already hit:

| Item | Status | Notes |
|---|---|---|
| Test the full flow against a self-hosted cognee | planned | Chat, citations, forget, ingest, repository indexing, dashboard, sync status and distillation, against a local cognee server (for example on `localhost:8000`). |
| No API key | planned | Self-hosted cognee can run with access control off. The client already sends no `X-Api-Key` when `COGNEE_API_KEY` is empty; check that every route works that way. |
| Ingest progress endpoint | planned | `/v1/datasets/status/progress` exists in the source but returns 404 on Cloud (finding #4). Use it where it exists, and fall back to `/v1/datasets/status` otherwise. |
| `/improve` result shape | planned | The source documents `stages[]` per stage; Cloud returns a pipeline run (finding #6). Handle both, and show per-stage results where they exist. |
| Server version differences | planned | Detect what the server supports at startup, and show it in the dashboard's Configuration panel (cognee version, which optional endpoints answer). cognee has no capability endpoint, so this means probing. |
| Automated tests for both | planned | The test suite runs against a fake client. Add a smoke test that can run against a real self-hosted server in CI, and against the tenant by hand. |

---

## Later / ideas

Not scoped yet; add freely.

- More than one replica. Needs the sync record and distillation status in shared
  storage instead of one process's memory and a local file.
- More sites than `demo`: a site id per customer site, each with its own datasets.
