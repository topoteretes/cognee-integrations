"""Embeddable web chat widget powered by cognee memory.

Run a tiny FastAPI backend that any site can talk to via a single ``<script>``
tag. It doubles as an "ask our docs" assistant: seed a docs corpus once and
every visitor conversation can recall from it, with inline **citations** to the
source material.

The backend is a thin, CORS-enabled proxy — all memory behavior lives in
``ChatMemoryAdapter`` (``adapter.py``), which talks to a running cognee server
over HTTP and never imports cognee in-process. Point it at that server with
``COGNEE_BASE_URL`` (and ``COGNEE_API_KEY`` if the server has auth on).

Run it::

    cognee-web-widget            # or: python -m cognee_integration_web_widget.server

Then open http://127.0.0.1:8000 for the "ask our docs" demo page, or embed the
widget on your own site::

    <script src="http://127.0.0.1:8000/widget.js"
            data-site-id="acme" data-api="http://127.0.0.1:8000"></script>

Endpoints:
    POST /api/chat    -> {answer, citations, session_id}
    POST /api/forget  -> clear one conversation's memory
    GET  /            -> demo "ask our docs" page
    GET  /widget.js   -> the embeddable snippet
"""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .adapter import ChatMemoryAdapter

STATIC_DIR = Path(__file__).resolve().parent / "static"

# A couple of "docs" so the demo returns something without external setup.
# Replace these with your real docs/site content.
DEMO_DOCS = [
    "Cognee is an open-source AI memory platform. It turns raw data into a "
    "knowledge graph that AI agents can recall from, replacing plain RAG "
    "with an Extract-Cognify-Load pipeline.",
    "You store data with remember(), build the graph, then query it with "
    "recall(). Each conversation is isolated by a session_id, so one user's "
    "chat never leaks into another's.",
    "Use forget() to delete memory for a conversation or dataset. Visitors "
    "can opt out of being remembered at any time.",
]

DEMO_SITE_ID = os.getenv("WIDGET_SITE_ID", "demo")

# Public root of the site whose pages were ingested. Set it and every citation
# links to the page; leave it unset and citations name the document only.
DOCS_BASE_URL = os.getenv("WIDGET_DOCS_BASE_URL", "").strip() or None

adapter = ChatMemoryAdapter(top_k=8, docs_base_url=DOCS_BASE_URL)


# Operator dashboard. Unset means the routes 404 — the dashboard is opt-in, so a
# stock deployment never exposes corpus contents or the visitor question log.
DASHBOARD_TOKEN = os.getenv("WIDGET_DASHBOARD_TOKEN", "").strip() or None


def _require_dashboard(token: Optional[str]) -> None:
    """Gate the dashboard routes.

    404 rather than 401 when the feature is off, so an unconfigured deployment
    is indistinguishable from one without the route at all. Compared with
    ``compare_digest`` so a wrong token cannot be recovered a byte at a time.
    """
    if DASHBOARD_TOKEN is None:
        raise HTTPException(status_code=404)
    if not token or not secrets.compare_digest(token, DASHBOARD_TOKEN):
        raise HTTPException(status_code=401, detail="bad or missing dashboard token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Seed the demo "ask our docs" corpus once, best-effort, on startup."""
    try:
        await adapter.ingest_docs(site_id=DEMO_SITE_ID, documents=DEMO_DOCS)
    except Exception as error:  # noqa: BLE001 - the demo should still boot
        print(f"[web_widget] docs seeding skipped: {error}")
    yield


app = FastAPI(title="cognee web chat widget", lifespan=lifespan)

# The widget is embedded cross-origin on customer sites, so the browser needs
# CORS on /api/*. "*" is the sensible default for a public docs assistant; set
# WIDGET_ALLOWED_ORIGINS (comma-separated) to restrict it.
_origins = [o.strip() for o in os.getenv("WIDGET_ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    conversation_id: str
    visitor_id: str = "anonymous"
    site_id: str = DEMO_SITE_ID
    opt_in: bool = True
    use_docs: bool = True


class ForgetRequest(BaseModel):
    conversation_id: str
    visitor_id: str = "anonymous"
    site_id: str = DEMO_SITE_ID


@app.post("/api/chat")
async def chat(req: ChatRequest) -> JSONResponse:
    conversation = adapter.conversation(
        site_id=req.site_id, visitor_id=req.visitor_id, conversation_id=req.conversation_id
    )

    # "/forget" is a first-class command, not a question to answer.
    if req.message.strip().lower() in ("/forget", "forget me"):
        try:
            await adapter.forget(conversation=conversation)
            answer_text = "Done — I've forgotten this conversation."
        except Exception:  # noqa: BLE001 - surface a friendly message, not a 500
            answer_text = "Sorry — I couldn't reach memory right now."
        return JSONResponse(
            {
                "answer": answer_text,
                "citations": [],
                "session_id": conversation.session_id,
            }
        )

    answer = await adapter.answer(
        conversation=conversation,
        query=req.message,
        remember=req.opt_in,
        use_docs=req.use_docs,
    )
    return JSONResponse(answer.as_dict())


@app.post("/api/forget")
async def forget(req: ForgetRequest) -> JSONResponse:
    conversation = adapter.conversation(
        site_id=req.site_id, visitor_id=req.visitor_id, conversation_id=req.conversation_id
    )
    try:
        cleared = await adapter.forget(conversation=conversation)
    except Exception:  # noqa: BLE001 - a failing backend must not 500 the widget
        cleared = False
    return JSONResponse({"cleared": bool(cleared), "session_id": conversation.session_id})


@app.get("/")
async def demo_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "demo.html")


@app.get("/widget.js")
async def widget_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "widget.js", media_type="application/javascript")


def _field(item, *names, default=""):
    """First present value among ``names`` — the APIs vary by endpoint."""
    if not isinstance(item, dict):
        return default
    for n in names:
        v = item.get(n)
        if v not in (None, ""):
            return v
    return default


async def _dashboard_data() -> dict:
    """Everything the dashboard shows, gathered read-only from cognee."""
    client = adapter.client
    docs_dataset = adapter.docs_dataset(DEMO_SITE_ID)

    datasets = await client.list_datasets()
    match = next(
        (d for d in datasets if isinstance(d, dict) and d.get("name") == docs_dataset), None
    )
    items = await client.dataset_data(str(match.get("id"))) if match else []
    history = await client.recall_history()

    # Recall history is the whole principal's, and it interleaves both sides of
    # each exchange: `user` rows are the questions asked, `system` rows the
    # answers returned. Keep the questions, and scope to the docs dataset so the
    # list is what visitors asked *this* widget rather than every recall the key
    # has ever run. If nothing carries the dataset id (older rows record none),
    # fall back to unscoped so the panel is never mysteriously empty.
    dataset_id = str(match.get("id")) if match else None
    asked = [h for h in history if isinstance(h, dict) and _field(h, "user") == "user"]
    scoped = [h for h in asked if dataset_id and str(_field(h, "datasetId")) == dataset_id]
    rows, is_scoped = (scoped, True) if scoped else (asked, False)
    rows.sort(key=lambda h: str(_field(h, "createdAt")), reverse=True)

    questions = []
    for h in rows[:25]:
        q = _field(h, "text", "query", "question")
        if q:
            questions.append({"query": str(q)[:300], "at": str(_field(h, "createdAt"))})

    return {
        "config": {
            "cognee_base_url": client.base_url,
            "authenticated": bool(client.api_key),
            "docs_dataset": docs_dataset,
            "site_id": DEMO_SITE_ID,
            # Seeding is unconditional here: DEMO_DOCS is written on every boot.
            # Surfaced because against a real corpus that is a bug, not a demo.
            "seeds_demo_docs": True,
            "allowed_origins": _origins or ["*"],
            "cloud_reachable": bool(datasets),
        },
        "corpus": {
            "dataset": docs_dataset,
            "dataset_id": dataset_id,
            "exists": match is not None,
            "item_count": len(items),
            "items": [
                {
                    "id": str(_field(i, "id")),
                    "name": str(_field(i, "name", "rawDataLocation"))[:120],
                    "created": str(_field(i, "createdAt")),
                    # "Last synced" is updatedAt: it moves when the item is
                    # re-ingested, not when the underlying page is edited.
                    "updated": str(_field(i, "updatedAt")),
                    "source": str(
                        (_field(i, "externalMetadata", default={}) or {})
                        .get("_cognee", {})
                        .get("source_uri", "")
                    ),
                }
                for i in items
            ],
            "all_datasets": [str(_field(d, "name")) for d in datasets],
        },
        "questions": questions,
        "questions_scoped_to_dataset": is_scoped,
    }


@app.get("/api/dashboard")
async def dashboard_data(token: Optional[str] = Query(default=None)) -> JSONResponse:
    _require_dashboard(token)
    return JSONResponse(await _dashboard_data())


@app.get("/api/dashboard/sessions")
async def dashboard_sessions(token: Optional[str] = Query(default=None)) -> JSONResponse:
    """Widget conversations, newest activity first.

    Sessions are filtered to this site's prefix: the key can see every session
    in the tenant, and an operator looking at the widget's dashboard wants the
    widget's conversations, not an agent's.
    """
    _require_dashboard(token)
    prefix = f"web:{DEMO_SITE_ID}:"
    sessions = [
        {
            "session_id": str(_field(x, "session_id")),
            "started_at": str(_field(x, "started_at")),
            "last_activity_at": str(_field(x, "last_activity_at", "ended_at")),
            # msg_count is not populated on the list endpoint; the per-session
            # detail carries it, so it is fetched on expand rather than shown here.
            "tokens_in": _field(x, "tokens_in", default=None),
            "tokens_out": _field(x, "tokens_out", default=None),
            "cost_usd": _field(x, "cost_usd", default=None),
        }
        for x in await adapter.client.list_sessions()
        if str(_field(x, "session_id")).startswith(prefix)
    ]
    sessions.sort(key=lambda x: x["last_activity_at"] or x["started_at"], reverse=True)
    return JSONResponse({"sessions": sessions})


@app.get("/api/dashboard/sessions/{session_id:path}")
async def dashboard_session(
    session_id: str, token: Optional[str] = Query(default=None)
) -> JSONResponse:
    """One conversation: every question with the answer it got."""
    _require_dashboard(token)
    if not session_id.startswith(f"web:{DEMO_SITE_ID}:"):
        raise HTTPException(status_code=404)
    detail = await adapter.client.session_detail(session_id)
    turns = [
        {
            "question": str(_field(q, "question")),
            "answer": str(_field(q, "answer")),
            "time": str(_field(q, "time")),
            "feedback_score": _field(q, "feedback_score", default=None),
            "feedback_text": str(_field(q, "feedback_text")),
        }
        for q in (detail.get("qas") or [])
    ]
    turns.sort(key=lambda t: t["time"])
    return JSONResponse({"session_id": session_id, "turns": turns})


@app.delete("/api/dashboard/data/{data_id}")
async def dashboard_delete_data(
    data_id: str, token: Optional[str] = Query(default=None)
) -> JSONResponse:
    """Permanently remove one ingested item from the docs corpus.

    Scoped to the widget's own dataset on purpose: the key can delete from any
    dataset it can write, and this dashboard should not be a way to reach the
    rest of the tenant.
    """
    _require_dashboard(token)
    datasets = await adapter.client.list_datasets()
    docs_dataset = adapter.docs_dataset(DEMO_SITE_ID)
    match = next(
        (d for d in datasets if isinstance(d, dict) and d.get("name") == docs_dataset), None
    )
    if not match:
        raise HTTPException(status_code=404, detail="docs dataset not found")
    ok = await adapter.client.delete_data(dataset_id=str(match.get("id")), data_id=data_id)
    if not ok:
        raise HTTPException(status_code=502, detail="cognee refused the delete")
    return JSONResponse({"deleted": data_id})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(token: Optional[str] = Query(default=None)) -> HTMLResponse:
    """The operator page.

    A shell only: it re-fetches /api/dashboard and the session routes from the
    browser, so acting on a row (deleting, opening a conversation) refreshes in
    place rather than reloading a server-rendered snapshot. The token travels in
    the query string it was opened with, so it is never stored by the page.
    """
    _require_dashboard(token)
    return HTMLResponse((STATIC_DIR / "dashboard.html").read_text(encoding="utf-8"))


def main() -> None:
    import uvicorn

    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
