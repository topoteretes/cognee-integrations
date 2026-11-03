"""Async HTTP transport to a running cognee server (self-hosted or Cloud).

Wire contract verified live against cognee 1.6.5 (fixtures in
``tests/contract/fixtures/``):

    POST /api/v1/datasets                      {name} -> dataset (create-or-return)
    GET  /api/v1/datasets                      -> [dataset]
    POST /api/v1/remember                      multipart data=<file>, datasetName,
                                               run_in_background=true -> {status, dataset_id}
    POST /api/v1/recall                        {query, datasets, scope:["graph"],
                                               search_type, top_k}
    GET  /api/v1/datasets/status?dataset=&pipeline=cognify_pipeline
    GET  /api/v1/datasets/{id}/data?limit=&offset=
    POST /api/v1/forget                        {datasetId, dataId}
    GET  /health

Auth is ``X-Api-Key`` only. Redirects are never followed with credentials,
responses are size-capped, one deadline covers headers and body, idempotent
reads retry once within their deadline, writes are never retried here (a
timed-out write has an uncertain outcome).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

import httpx

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_RETRY_DELAY_S = 0.5
LIST_PAGE_SIZE = 100
#: Safety cap on one listing walk (50,000 documents). Past it a lookup reports
#: ``exhausted`` rather than pretending the rest of the dataset is empty.
LIST_PAGE_MAX = 500
#: One deadline over a whole listing walk, on top of the per-page timeout. A walk
#: that runs out of time also reports ``exhausted``.
LIST_WALK_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class DataItemLookup:
    """Outcome of looking one document up by name."""

    status: Literal["found", "absent", "exhausted"]
    item: Optional[dict[str, Any]] = None
    #: Why an ``exhausted`` walk stopped (page cap or deadline); empty otherwise.
    reason: str = ""

    @property
    def data_id(self) -> Optional[str]:
        value = self.item.get("id") if self.item else None
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class DataItemListing:
    """One walk over a dataset listing: ``{name: row}`` and whether it reached the end."""

    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    complete: bool = False
    pages: int = 0
    #: Why an incomplete walk stopped (page cap or deadline); empty when complete
    #: or when it stopped early because every wanted name had been seen.
    reason: str = ""

    def lookup(self, name: str) -> DataItemLookup:
        item = self.items.get(name)
        if item is not None:
            return DataItemLookup(status="found", item=item)
        if self.complete:
            return DataItemLookup(status="absent")
        return DataItemLookup(status="exhausted", reason=self.reason)


class CogneeError(Exception):
    """Anything that went wrong talking to cognee, normalized."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status: int = 0,
        method: str = "",
        path: str = "",
        detail: str = "",
    ):
        super().__init__(message)
        self.kind = kind  # http | timeout | network | malformed | redirect | config
        self.status = status
        self.method = method
        self.path = path
        self.detail = detail[:300]

    @property
    def outcome_uncertain(self) -> bool:
        return self.kind in {"timeout", "network"}

    @property
    def is_auth(self) -> bool:
        return self.kind == "http" and self.status in (401, 403)

    @property
    def is_not_found(self) -> bool:
        return self.kind == "http" and self.status == 404

    @property
    def is_rate_limited(self) -> bool:
        return self.kind == "http" and self.status == 429

    @property
    def is_server_error(self) -> bool:
        return self.kind == "http" and self.status >= 500

    @property
    def is_no_searchable_memory(self) -> bool:
        return self.is_not_found and bool(
            re.search(r"no searchable memory|no data has been added|NoDataError", self.detail, re.I)
        )

    def describe(self) -> str:
        where = f" ({self.method} {self.path})" if self.method else ""
        status = f" HTTP {self.status}" if self.status else ""
        detail = f": {self.detail}" if self.detail else ""
        return f"{self.kind}{status}{where}{detail}"[:400]


def require_uuid(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not _UUID.match(value):
        raise CogneeError(f"{label} is not a UUID", kind="config")
    return value


def _extract_detail(parsed: Any, text: str) -> str:
    if isinstance(parsed, dict):
        for key in ("detail", "error", "message"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, (dict, list)):
                return json.dumps(value)[:300]
    return text[:300]


class CogneeHttpBackend:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        request_timeout_s: float = 15.0,
        max_response_bytes: int = 4 * 1024 * 1024,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._request_timeout_s = request_timeout_s
        self._max_response_bytes = max_response_bytes
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> "CogneeHttpBackend":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # -- health / datasets ----------------------------------------------------

    async def health(self, *, timeout_s: Optional[float] = None) -> dict[str, Any]:
        status, body = await self._raw(
            "GET", "/health", auth=False, timeout_s=timeout_s, any_status=True
        )
        return {"reachable": status > 0, "status": status, "body": body}

    async def list_datasets(self, *, timeout_s: Optional[float] = None) -> list[dict[str, Any]]:
        body = await self._request("GET", "/api/v1/datasets", timeout_s=timeout_s, retries=1)
        return [row for row in body if isinstance(row, dict)] if isinstance(body, list) else []

    async def ensure_dataset(
        self, name: str, *, timeout_s: Optional[float] = None
    ) -> dict[str, Any]:
        body = await self._request(
            "POST", "/api/v1/datasets", json={"name": name}, timeout_s=timeout_s
        )
        if not isinstance(body, dict) or not isinstance(body.get("id"), str):
            raise CogneeError(
                "dataset create returned no id",
                kind="malformed",
                method="POST",
                path="/api/v1/datasets",
            )
        return body

    # -- writes ---------------------------------------------------------------

    async def remember(
        self, *, dataset: str, filename: str, text: str, timeout_s: Optional[float] = None
    ) -> dict[str, Any]:
        """Background permanent write. No document id comes back; find it later by ``filename``."""
        files = {"data": (filename, text.encode("utf-8"), "text/plain")}
        data = {"datasetName": dataset, "run_in_background": "true"}
        body = await self._request(
            "POST", "/api/v1/remember", files=files, data=data, timeout_s=timeout_s, retries=0
        )
        return body if isinstance(body, dict) else {}

    async def forget_document(
        self, *, dataset_id: str, data_id: str, timeout_s: Optional[float] = None
    ) -> dict[str, Any]:
        """Delete exactly one document. The server says ``success`` even for an unknown id."""
        body = await self._request(
            "POST",
            "/api/v1/forget",
            json={
                "datasetId": require_uuid(dataset_id, "dataset id"),
                "dataId": require_uuid(data_id, "data id"),
            },
            timeout_s=timeout_s,
            retries=0,
        )
        return body if isinstance(body, dict) else {}

    # -- reads ----------------------------------------------------------------

    async def recall(
        self,
        *,
        query: str,
        datasets: list[str],
        top_k: int,
        search_type: str = "CHUNKS",
        timeout_s: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        if not datasets:
            raise CogneeError("recall needs at least one dataset", kind="config")
        payload = {
            "query": query,
            "datasets": datasets,
            "scope": ["graph"],
            "search_type": search_type,
            "top_k": top_k,
        }
        body = await self._request(
            "POST", "/api/v1/recall", json=payload, timeout_s=timeout_s, retries=1
        )
        if isinstance(body, list):
            return [row for row in body if isinstance(row, dict)]
        if isinstance(body, dict):
            return [body]
        return []

    async def dataset_status(self, dataset_id: str, *, timeout_s: Optional[float] = None) -> str:
        dataset_id = require_uuid(dataset_id, "dataset id")
        body = await self._request(
            "GET",
            "/api/v1/datasets/status",
            params={"dataset": dataset_id, "pipeline": "cognify_pipeline"},
            timeout_s=timeout_s,
            retries=1,
        )
        if not isinstance(body, dict):
            return ""
        value = body.get(dataset_id)
        if value is None and len(body) == 1:
            value = next(iter(body.values()))
        if isinstance(value, dict):
            value = value.get("cognify_pipeline")
        return str(value).upper() if isinstance(value, str) else ""

    async def list_dataset_data(
        self,
        dataset_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        timeout_s: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        dataset_id = require_uuid(dataset_id, "dataset id")
        body = await self._request(
            "GET",
            f"/api/v1/datasets/{dataset_id}/data",
            params={"limit": limit, "offset": offset},
            timeout_s=timeout_s,
            retries=1,
        )
        return [row for row in body if isinstance(row, dict)] if isinstance(body, list) else []

    async def find_data_item(
        self,
        dataset_id: str,
        name: str,
        *,
        max_pages: int = LIST_PAGE_MAX,
        timeout_s: Optional[float] = None,
        deadline_s: float = LIST_WALK_TIMEOUT_S,
    ) -> DataItemLookup:
        """Find one document by upload name. ``exhausted`` is not ``absent``.

        The listing is walked until a short page proves the end. Hitting
        ``max_pages`` or ``deadline_s`` first means nothing is known about the
        remainder, and callers must not read that as "not there".
        """
        listing = await self.list_data_items(
            dataset_id,
            wanted={name},
            max_pages=max_pages,
            timeout_s=timeout_s,
            deadline_s=deadline_s,
        )
        return listing.lookup(name)

    async def list_data_items(
        self,
        dataset_id: str,
        *,
        wanted: Optional[set[str]] = None,
        max_pages: int = LIST_PAGE_MAX,
        timeout_s: Optional[float] = None,
        deadline_s: float = LIST_WALK_TIMEOUT_S,
    ) -> DataItemListing:
        """Page a dataset's documents once into ``{name: row}``.

        ``timeout_s`` bounds each page, ``deadline_s`` the whole walk. With
        ``wanted`` only those rows are kept and the walk stops as soon as every
        wanted name has been seen; ``complete`` then only says whether the walk
        reached a short page.
        """
        limit = LIST_PAGE_SIZE
        per_page = timeout_s if timeout_s is not None else self._request_timeout_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_s
        items: dict[str, dict[str, Any]] = {}
        pages = 0
        out_of_time = f"listing walk exceeded its {deadline_s:.0f}s deadline"
        for page in range(max_pages):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return DataItemListing(
                    items=items,
                    complete=False,
                    pages=pages,
                    reason=f"{out_of_time} after {pages} pages",
                )
            try:
                rows = await self.list_dataset_data(
                    dataset_id,
                    limit=limit,
                    offset=page * limit,
                    timeout_s=min(per_page, remaining),
                )
            except CogneeError as exc:
                if exc.kind == "timeout" and pages > 0 and loop.time() >= deadline:
                    return DataItemListing(
                        items=items,
                        complete=False,
                        pages=pages,
                        reason=f"{out_of_time} after {pages} pages",
                    )
                raise
            pages += 1
            for row in rows:
                name = row.get("name")
                if not isinstance(name, str) or name in items:
                    continue
                if wanted is not None and name not in wanted:
                    continue
                items[name] = row
            if len(rows) < limit:
                return DataItemListing(items=items, complete=True, pages=pages)
            if wanted is not None and wanted <= items.keys():
                return DataItemListing(items=items, complete=False, pages=pages)
        return DataItemListing(
            items=items,
            complete=False,
            pages=pages,
            reason=f"dataset listing exceeds the {max_pages * LIST_PAGE_SIZE}-document cap",
        )

    # -- transport ------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        files: Any = None,
        data: Any = None,
        params: Any = None,
        timeout_s: Optional[float] = None,
        retries: int = 0,
    ) -> Any:
        total = timeout_s if timeout_s is not None else self._request_timeout_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + total
        attempt = 0
        while True:
            remaining = deadline - loop.time()
            if attempt > 0 and remaining <= 0:
                raise CogneeError(
                    f"{method} {path} timed out after retries",
                    kind="timeout",
                    method=method,
                    path=path,
                )
            try:
                # ``httpx.Timeout`` bounds each connect/read/write separately, so a
                # body that trickles in one byte at a time would never trip it. The
                # deadline is enforced over the whole exchange here, headers and body.
                budget = total if attempt == 0 else remaining
                try:
                    status, body = await asyncio.wait_for(
                        self._raw(
                            method,
                            path,
                            auth=True,
                            json=json,
                            files=files,
                            data=data,
                            params=params,
                            timeout_s=budget,
                        ),
                        timeout=budget,
                    )
                except asyncio.TimeoutError:
                    raise CogneeError(
                        f"{method} {path} exceeded its {budget:.1f}s deadline",
                        kind="timeout",
                        method=method,
                        path=path,
                    ) from None
                if isinstance(body, dict) and isinstance(body.get("error"), str) and status < 400:
                    raise CogneeError(
                        f"{method} {path} answered an error",
                        kind="http",
                        status=500,
                        method=method,
                        path=path,
                        detail=body["error"],
                    )
                return body
            except CogneeError as exc:
                retryable = (
                    exc.kind in {"network", "timeout"} or exc.is_server_error or exc.is_rate_limited
                )
                if not retryable or attempt >= retries:
                    raise
                attempt += 1
                await asyncio.sleep(
                    min(_RETRY_DELAY_S * (2 ** (attempt - 1)), max(0.0, deadline - loop.time()))
                )

    async def _raw(
        self,
        method: str,
        path: str,
        *,
        auth: bool,
        json: Any = None,
        files: Any = None,
        data: Any = None,
        params: Any = None,
        timeout_s: Optional[float] = None,
        any_status: bool = False,
    ) -> tuple[int, Any]:
        headers: dict[str, str] = {}
        if auth and self._api_key:
            headers["X-Api-Key"] = self._api_key
        timeout = httpx.Timeout(timeout_s if timeout_s is not None else self._request_timeout_s)
        try:
            async with self._client.stream(
                method,
                path,
                json=json,
                files=files,
                data=data,
                params=params,
                headers=headers,
                timeout=timeout,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise CogneeError(
                        f"{method} {path} redirected (HTTP {response.status_code}); "
                        "credentials are not forwarded across origins",
                        kind="redirect",
                        status=response.status_code,
                        method=method,
                        path=path,
                    )
                declared = int(response.headers.get("content-length") or 0)
                if declared > self._max_response_bytes:
                    raise CogneeError(
                        f"{method} {path} response too large",
                        kind="malformed",
                        method=method,
                        path=path,
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self._max_response_bytes:
                        raise CogneeError(
                            f"{method} {path} response too large",
                            kind="malformed",
                            method=method,
                            path=path,
                        )
                    chunks.append(chunk)
                text = b"".join(chunks).decode("utf-8", errors="replace")
                status = response.status_code
        except CogneeError:
            raise
        except httpx.TimeoutException as exc:
            raise CogneeError(
                f"{method} {path} timed out", kind="timeout", method=method, path=path
            ) from exc
        except httpx.HTTPError as exc:
            raise CogneeError(
                f"{method} {path} failed: {str(exc)[:200]}",
                kind="network",
                method=method,
                path=path,
            ) from exc

        parsed: Any = None
        if text:
            try:
                parsed = _json_loads(text)
            except ValueError:
                if status < 400 and not any_status:
                    raise CogneeError(
                        f"{method} {path} returned malformed JSON",
                        kind="malformed",
                        method=method,
                        path=path,
                        detail=text[:120],
                    ) from None
                parsed = text
        if status >= 400 and not any_status:
            raise CogneeError(
                f"{method} {path} failed (HTTP {status})",
                kind="http",
                status=status,
                method=method,
                path=path,
                detail=_extract_detail(parsed, text),
            )
        return status, parsed


def _json_loads(text: str) -> Any:
    return json.loads(text)
