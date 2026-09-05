import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import ingest
from .config import Config, load_config
from .manifest import build_openapi_spec
from .tapes_client import TapesClient

logger = logging.getLogger(__name__)


class SyncRequest(BaseModel):
    full: bool = False
    wait: bool = False


class SearchRequest(BaseModel):
    query: str
    search_type: str = "GRAPH_COMPLETION"
    top_k: int = Field(default=10, ge=1)


def create_app(config: Config | None = None, tapes: TapesClient | None = None) -> FastAPI:
    config = config or load_config()
    tapes = tapes or TapesClient(config.tapes_base_url)
    syncer = ingest.Syncer(config, tapes)

    ingest.apply_storage_isolation(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await tapes.aclose()

    # The tapes manifest at /openapi is the public contract; FastAPI's own
    # spec/docs endpoints are disabled so there is exactly one.
    app = FastAPI(
        title="Cognee Cassette",
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.config = config
    app.state.syncer = syncer

    @app.get("/ping")
    async def ping() -> dict:
        return {"status": "ok"}

    @app.get("/openapi")
    async def openapi_spec() -> dict:
        return build_openapi_spec(config)

    @app.post("/api/sync")
    async def sync(request: SyncRequest | None = None) -> dict:
        request = request or SyncRequest()
        if request.wait:
            if syncer.is_running():
                return {"accepted": False, "status": syncer.status.snapshot()}
            status = await syncer.run(full=request.full)
            return {"accepted": True, "status": status.snapshot()}
        accepted = syncer.start(full=request.full)
        return {"accepted": accepted, "status": syncer.status.snapshot()}

    @app.post("/api/sync/status")
    async def sync_status() -> dict:
        return syncer.status.snapshot()

    @app.post("/api/search")
    async def search(request: SearchRequest) -> dict:
        try:
            results = await ingest.search(config, request.query, request.search_type, request.top_k)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"results": results}

    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config()
    logger.info(
        "Starting cognee cassette on %s:%d (tapes: %s, dataset: %s)",
        config.host,
        config.port,
        config.tapes_base_url,
        config.dataset_name,
    )
    uvicorn.run(create_app(config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
