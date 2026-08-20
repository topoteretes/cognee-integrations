"""HTTP API the macOS app talks to.

Search merges two sources the way system launchers do: instant filename matches
from the catalog (available the moment a folder is indexed) and semantic
chunk hits from cognee (available once cognify finishes), deduplicated by
path with the filename hit winning on score ties. ``mode=answer`` instead
asks cognee's graph for a direct answer to a question.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .adapters import chunk_text, extract_file_hint, make_adapter
from .catalog import Catalog
from .config import Settings, env
from .handover import HandoverService, make_handover
from .indexer import Indexer

SEMANTIC_BASE_SCORE = 50.0  # below exact/prefix name hits, above weak subsequence ones

# Time budget for the semantic half of a files-mode search. While cognify is
# chewing through a big folder, cognee's stores can stall a query for minutes;
# the panel must still get its instant filename results. Sized to fit cognee's
# per-query worker spawn (~3s) with headroom.
SEMANTIC_TIMEOUT_SECONDS = 12.0


class IndexRequest(BaseModel):
    paths: list[str]
    extensions: list[str] = []  # optional: only index these types under paths


class ShareRequest(BaseModel):
    to: str  # a username, "team:<name>", or "org"
    title: str
    body: str
    source: str = ""


class SeenRequest(BaseModel):
    ids: list[str]


class FeedbackRequest(BaseModel):
    query: str
    answer: str
    rating: int  # 1-5; >=4 reinforces memory


class CaptureRequest(BaseModel):
    text: str
    title: str = ""
    source: str = ""  # e.g. "quick-capture", "git:repo-name", "share-sheet"


class ForgetRequest(BaseModel):
    path: str  # an indexed file, or a whole watched root


def create_app(
    settings: Optional[Settings] = None,
    adapter: Any = None,
    handover: Optional[HandoverService] = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    adapter = adapter or make_adapter(settings)
    catalog = Catalog(settings.data_dir / "catalog.json")
    indexer = Indexer(adapter, catalog, settings)
    handover = handover or make_handover(settings, adapter)

    app = FastAPI(title="Cognee backend")
    app.state.settings = settings
    app.state.catalog = catalog
    app.state.indexer = indexer
    app.state.handover = handover

    from .experiments import ConversationThreads, is_temporal
    from .sources import SourceManager

    threads = ConversationThreads()
    source_manager = SourceManager.from_env(indexer, settings.data_dir)
    app.state.sources = source_manager

    def _source_items(s: Any) -> tuple[list[str], int]:
        """What a source has actually brought in: connectors list their
        staged documents (newest first); the folder source lists the
        indexed roots. Capped for display — ``count`` carries the total."""
        staging = getattr(s, "staging", None)
        if staging is None:
            roots = list(catalog.roots)
            return roots, len(roots)
        if not staging.exists():
            return [], 0
        files = sorted(
            (p for p in staging.rglob("*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [str(p.relative_to(staging)) for p in files[:12]], len(files)

    @app.get("/sources")
    async def sources() -> dict:
        """Each source describes itself (label + SF Symbol icon) and reports
        what it indexed and when it last synced, so the app renders whatever
        connectors exist — with real insight — without hardcoding any."""
        described = []
        for s in source_manager.sources:
            status = source_manager.status.get(s.name, {})
            items, count = _source_items(s)
            described.append(
                {
                    "name": s.name,
                    "label": getattr(s, "label", s.name.title()),
                    "icon": getattr(s, "icon", "puzzlepiece.extension"),
                    "ok": status.get("ok"),
                    "detail": status.get("detail", ""),
                    "at": status.get("at"),
                    "scope": list(getattr(s, "scope", [])),
                    "items": items,
                    "count": count,
                }
            )
        return {"sources": described, "interval": source_manager.interval}

    @app.get("/files")
    async def files(limit: int = 500, q: str = "") -> dict:
        """Every file in the index, newest first. ``q`` substring-filters by
        path, so a large index stays browsable in the app's Settings."""
        if q:
            needle = q.lower()
            matching = [
                e for e in catalog.entries(limit=len(catalog)) if needle in e["path"].lower()
            ]
            return {"files": matching[:limit], "total": len(catalog), "matched": len(matching)}
        entries = catalog.entries(limit=limit)
        return {"files": entries, "total": len(catalog), "matched": len(catalog)}

    @app.post("/files/forget")
    async def forget_file(request: ForgetRequest) -> dict:
        """Remove a file (or a whole watched root) from the index.

        Deliberately cautious about the graph side: a single file's data
        item is deleted from its dataset when it can be identified
        unambiguously; anything bulk — a root's many files, an ambiguous
        name — leaves the graph copy alone (mass dataset deletion crashed
        a cloud tenant once; that is a manual, eyes-open operation).
        The file always leaves the catalog, so it stops appearing in
        results and counts either way.
        """
        from pathlib import Path as _Path

        path = request.path.strip()
        if not path:
            return {"ok": False, "removed": 0, "graph": "kept", "detail": "empty path"}
        if path in catalog.roots and _Path(path).is_dir():
            removed = catalog.remove_root(path)
            catalog.save()
            return {
                "ok": True,
                "removed": removed,
                "graph": "kept",
                "detail": "root unwatched; graph copies kept (bulk deletion is manual)",
            }
        # a single file — possibly a root of its own, when it was indexed
        # directly — gets the per-item treatment, graph deletion included
        removed = (
            catalog.remove_root(path)
            if path in catalog.roots
            else (1 if catalog.remove(path) else 0)
        )
        if not removed:
            return {"ok": False, "removed": 0, "graph": "kept", "detail": "not in the index"}
        catalog.save()
        graph, detail = "kept", "graph copy kept"
        if hasattr(adapter, "_request"):
            try:
                dataset_name = indexer.dataset_for(path) or settings.dataset
                listing = await adapter._request("GET", "/api/v1/datasets")
                datasets = listing.json() if isinstance(listing.json(), list) else []
                ds = next((d for d in datasets if str(d.get("name")) == dataset_name), None)
                if ds is not None:
                    data = await adapter._request("GET", f"/api/v1/datasets/{ds['id']}/data")
                    items = data.json() if isinstance(data.json(), list) else []
                    # the tenant names data items without the extension
                    wanted = {_Path(path).name, _Path(path).stem}
                    matches = [i for i in items if str(i.get("name", "")) in wanted]
                    if len(matches) == 1:
                        response = await adapter._request(
                            "DELETE",
                            f"/api/v1/datasets/{ds['id']}/data/{matches[0]['id']}",
                        )
                        if response.status_code < 400:
                            graph, detail = "deleted", "graph copy deleted"
                        else:
                            detail = f"graph delete failed (HTTP {response.status_code})"
                    elif len(matches) > 1:
                        detail = "graph copy kept (name is ambiguous in the dataset)"
                    else:
                        detail = "graph copy kept (no matching data item)"
            except Exception as exc:
                detail = f"graph copy kept ({type(exc).__name__})"
        return {"ok": True, "removed": 1, "graph": graph, "detail": detail}

    @app.get("/whisper")
    async def whisper(q: str) -> dict:
        """Memory talking back while a note is typed: the closest thing it
        already knows, and (experiments) whether the note conflicts with
        recorded facts. Backs the quick-capture whisper line."""
        q = q.strip()
        if len(q) < 12:  # too little signal to be worth a lookup
            return {"related": [], "conflicts": []}
        related: list[str] = []
        try:
            for chunk in await adapter.chunks(q, top_k=2):
                if text := chunk_text(chunk):
                    related.append(_trim(text, limit=140))
        except Exception:
            pass
        conflicts: list = []
        if settings.experiments:
            from .experiments import contradictions_for

            try:
                conflicts = (await contradictions_for(adapter, q))[:1]
            except Exception:
                conflicts = []
        return {"related": related[:2], "conflicts": conflicts}

    @app.get("/digest")
    async def digest(since: float = 0) -> dict:
        """How much the agent-session layer grew since ``since`` (unix time).
        Backs the "your agents learned N things" notification."""
        if not hasattr(adapter, "_request") or since <= 0:
            return {"count": 0, "titles": []}
        try:
            listing = await adapter._request("GET", "/api/v1/datasets")
            datasets = listing.json() if isinstance(listing.json(), list) else []
            ds = next(
                (
                    d
                    for d in datasets
                    if "agent" in str(d.get("name", "")) or "session" in str(d.get("name", ""))
                ),
                None,
            )
            if ds is None:
                return {"count": 0, "titles": []}
            data = await adapter._request("GET", f"/api/v1/datasets/{ds['id']}/data")
            items = data.json() if isinstance(data.json(), list) else []
            fresh = [
                str(item.get("name", "")) or "learning"
                for item in items
                if _iso_to_unix(str(item.get("created_at", ""))) > since
            ]
            return {"count": len(fresh), "titles": fresh[:5]}
        except Exception:
            return {"count": 0, "titles": []}

    @app.on_event("startup")
    async def start_sources() -> None:
        source_manager.start()

    @app.on_event("startup")
    async def warm_semantic_search() -> None:
        """Pay the cold-start cost (vector store open, embedding client init)
        once at boot, unbudgeted, so the first real search fits its budget."""

        async def warm() -> None:
            try:
                await adapter.chunks("warmup", top_k=1)
            except Exception:
                pass

        asyncio.get_running_loop().create_task(warm())

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "mode": settings.mode,
            "dataset": settings.dataset,
            "indexed_files": len(catalog),
            "index": indexer.status,
            "experiments": settings.experiments,
            "handover": (
                {"user": handover.config.user, "team": handover.config.team} if handover else None
            ),
        }

    @app.post("/share")
    async def share(request: ShareRequest) -> dict:
        if handover is None:
            return {
                "ok": False,
                "error": "handover not configured (set COGNEE_DESKTOP_USER and COGNEE_HUB_URL)",
            }
        return await handover.share(request.to, request.title, request.body, request.source)

    @app.get("/inbox")
    async def inbox() -> dict:
        if handover is None:
            return {"items": [], "unseen": 0, "enabled": False}
        return {**(await handover.inbox()), "enabled": True}

    @app.post("/inbox/seen")
    async def inbox_seen(request: SeenRequest) -> dict:
        if handover is not None:
            handover.mark_seen(request.ids)
        return {"ok": True}

    @app.get("/graph", response_class=HTMLResponse)
    async def graph(dataset: str = "", max_nodes: int = 350, query: str = "") -> str:
        """Interactive view of the knowledge graph behind this backend.

        Serves cognee's own visualization page (one dataset at a time, with a
        layer-switcher bar); falls back to the built-in canvas renderer when
        the server's visualize route is unavailable."""
        from .graphview import collect_graph, native_visualization, render_html

        if not hasattr(adapter, "_request"):
            if settings.mode == "local":
                # local cognee renders its own visualization in-process
                try:
                    import inspect

                    import cognee

                    html = cognee.visualize_graph(dataset=dataset or settings.dataset)
                    if inspect.isawaitable(html):
                        html = await html
                    if isinstance(html, str) and "<" in html:
                        return html
                except Exception:
                    pass
            return (
                "<html><body style='font-family:sans-serif;padding:40px'>"
                "<h2>Graph view is unavailable</h2><p>This backend runs "
                f"in <b>{settings.mode}</b> mode and its graph could not be "
                "rendered.</p></body></html>"
            )
        native = await native_visualization(
            adapter, dataset, max_nodes, query, exclude=settings.exclude_datasets
        )
        if native is not None:
            return native
        data = await collect_graph(adapter, max_nodes=max_nodes)
        return render_html(data)

    @app.post("/index", status_code=202)
    async def index(request: IndexRequest) -> dict:
        started = indexer.start(request.paths, extensions=request.extensions)
        return {"started": started, "roots": catalog.roots}

    @app.get("/index/status")
    async def index_status() -> dict:
        return {
            **indexer.status,
            "roots": catalog.roots,
            "root_filters": catalog.root_filters,
            "indexed_files": len(catalog),
        }

    # cognee spawns a DB worker per query; concurrent queries contend on the
    # store's single-writer lock. Two in flight keeps typing bursts sane.
    semantic_gate = asyncio.Semaphore(2)

    # Answer/semantic responses are cached briefly: a cloud-tenant search
    # costs seconds, and repeating a query (rehearsal, back-and-forth in a
    # conversation) shouldn't pay it twice. Index runs don't invalidate the
    # cache — the TTL bounds staleness instead, keeping the path simple.
    cache_ttl = float(env("SEARCH_CACHE_TTL", "600"))
    search_cache: dict[tuple, tuple[float, dict]] = {}

    def cache_get(key: tuple) -> Optional[dict]:
        hit = search_cache.get(key)
        if hit and time.time() - hit[0] < cache_ttl:
            return hit[1]
        return None

    def cache_put(key: tuple, value: dict) -> None:
        if len(search_cache) > 200:
            search_cache.pop(next(iter(search_cache)))
        search_cache[key] = (time.time(), value)

    @app.post("/capture", status_code=202)
    async def capture(request: CaptureRequest) -> dict:
        """One thought (or commit, or shared selection) into memory.

        The note lands in the capture folder and indexes like any document —
        quick-capture hotkey, the share CLI, and the git hook all funnel here.
        """
        import re as _re
        import time as _time

        text = request.text.strip()
        if not text:
            return {"ok": False, "detail": "empty"}
        title = request.title.strip() or text.splitlines()[0][:60]
        slug = _re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-") or "note"
        capture_dir = settings.data_dir / "capture"
        capture_dir.mkdir(parents=True, exist_ok=True)
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        path = capture_dir / f"{stamp}-{slug}.md"
        header = f"# {title}\n\n"
        if request.source:
            header += f"- captured from: {request.source}\n"
        header += f"- captured at: {_time.strftime('%Y-%m-%d %H:%M')}\n\n"
        path.write_text(header + text + "\n")
        indexer.start([str(capture_dir)])
        return {"ok": True, "path": str(path)}

    @app.post("/feedback")
    async def feedback(request: FeedbackRequest) -> dict:
        if not settings.experiments:
            return {"ok": False, "detail": "experiments off"}
        from .experiments import record_feedback

        outcome = await record_feedback(
            adapter, settings.data_dir, request.query, request.answer, request.rating
        )
        return {"ok": True, "outcome": outcome}

    @app.get("/experts")
    async def experts(q: str, limit: int = 5) -> dict:
        """Who knows about this — people ranked by their footprint in the
        matching memory (chunk provenance + handover senders)."""
        from .experts import find_experts

        return {"query": q, "experts": await find_experts(adapter, q, limit=limit)}

    @app.get("/search")
    async def search(
        q: str, mode: str = "files", limit: int = 12, semantic: int = 1, thread: str = ""
    ) -> dict:
        q = q.strip()
        if not q:
            return {"query": q, "answer": None, "results": []}

        # scoped by principal: one process serves one user today, but a
        # cache key that ignores identity is a data leak waiting for the
        # first shared deployment
        principal = settings.user or settings.dataset
        cache_key = (principal, q.lower(), mode, limit, bool(semantic))
        if mode != "answer" and (cached := cache_get(cache_key)) is not None:
            return cached

        if mode == "answer":
            effective_q = threads.contextualize(thread, q) if thread else q
            cache_key = (principal, effective_q.lower(), mode, limit, bool(semantic))
            if (cached := cache_get(cache_key)) is not None:
                return cached
            search_type = "GRAPH_COMPLETION"
            if settings.experiments and is_temporal(q):
                search_type = "TEMPORAL"
            if hasattr(adapter, "answer_with_sources"):
                meta = await adapter.answer_with_sources(effective_q, search_type=search_type)
                if not meta.get("answer") and search_type != "GRAPH_COMPLETION":
                    # not every deployment supports TEMPORAL (cognee cloud
                    # returns 400) — degrade to the plain graph answer
                    meta = await adapter.answer_with_sources(effective_q)
                answer = meta["answer"]
                sources = [
                    {"dataset": name, "layer": _layer_label(name, settings.dataset)}
                    for name in meta["sources"]
                ]
            else:
                answer = await adapter.answer(effective_q)
                sources = []
            contradictions: list = []
            if settings.experiments and answer:
                from .experiments import contradictions_for

                try:
                    contradictions = (await contradictions_for(adapter, q))[:3]
                except Exception:
                    contradictions = []
            if thread and answer:
                threads.remember_turn(thread, q, answer)
            response = {
                "query": q,
                "answer": answer or None,
                "sources": sources,
                "contradictions": contradictions,
                "results": [],
            }
            if answer:
                cache_put(cache_key, response)
            return response

        results = [
            {
                "kind": "file",
                "title": hit["name"],
                "path": hit["path"],
                "snippet": "",
                "score": hit["score"],
                "source": "filename",
            }
            for hit in catalog.match_names(q, limit=limit)
        ]
        # semantic=0 is the app's per-keystroke request: instant names only.
        if not semantic:
            return {"query": q, "answer": None, "results": results[:limit]}

        seen = {r["path"] for r in results}

        async def gated_chunks() -> list:
            # During a typing burst, stale queries must not queue up behind the
            # gate — the newest query wins; a query that can't start promptly
            # returns nothing rather than starving its successors.
            try:
                await asyncio.wait_for(semantic_gate.acquire(), timeout=2.0)
            except TimeoutError:
                return []
            try:
                return await adapter.chunks(q, top_k=limit)
            finally:
                semantic_gate.release()

        # Budgeted but never cancelled: cancelling a cognee store call mid-flight
        # can leave its locks poisoned, hanging every later search in the
        # process. shield() lets a slow call finish in the background (result
        # discarded) while the panel gets its filename results on time.
        chunks_task = asyncio.ensure_future(gated_chunks())
        chunks_task.add_done_callback(_swallow)
        try:
            chunks = await asyncio.wait_for(
                asyncio.shield(chunks_task), timeout=SEMANTIC_TIMEOUT_SECONDS
            )
        except Exception:  # semantic search slow or down != no search at all
            chunks = []
        for chunk in chunks:
            text = chunk_text(chunk)
            if not text:
                continue
            path = _resolve_path(chunk, catalog)
            if path in seen:
                # enrich the filename hit with a snippet instead of duplicating
                for r in results:
                    if r["path"] == path and not r["snippet"]:
                        r["snippet"] = _trim(text)
                continue
            if path:
                seen.add(path)
            results.append(
                {
                    "kind": "file" if path else "snippet",
                    "title": (path or "").rsplit("/", 1)[-1] or "Matching passage",
                    "path": path or "",
                    "snippet": _trim(text),
                    "score": SEMANTIC_BASE_SCORE,
                    "source": "semantic",
                }
            )
        results.sort(key=lambda r: r["score"], reverse=True)
        response = {"query": q, "answer": None, "results": results[:limit]}
        # only cache complete responses: a timed-out semantic half would pin
        # filename-only results for the TTL
        if not semantic or chunks:
            cache_put(cache_key, response)
        return response

    return app


def _layer_label(dataset: str, own: str = "") -> str:
    """Friendly memory-layer name for answer attribution."""
    if dataset.startswith("handover-") or dataset.startswith("team-") or dataset == "org-memory":
        return "team handover"
    if dataset.startswith("github-"):
        return "github"
    if "agent" in dataset or "session" in dataset:
        return "agent session"
    if dataset == (own or "main"):
        return "your files"
    return "documents"


def _swallow(task: "asyncio.Task") -> None:
    """Consume a background task's exception so it never logs as unhandled."""
    if not task.cancelled():
        task.exception()


def _resolve_path(chunk: dict, catalog: Catalog) -> str:
    hint = extract_file_hint(chunk)
    if not hint:
        return ""
    if hint.startswith("/"):
        return hint
    if hint.startswith("file://"):
        return hint[len("file://") :]
    return catalog.find_by_basename(hint.rsplit("/", 1)[-1]) or ""


def _trim(text: str, limit: int = 220) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _iso_to_unix(iso: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
