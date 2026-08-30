import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)


def parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_newer(last_seen_at: str, since: str) -> bool:
    try:
        return parse_ts(last_seen_at) > parse_ts(since)
    except ValueError:
        # Fall back to string comparison for timestamps we can't parse.
        return last_seen_at >= since


class TapesClient:
    """Thin async client for the tapes core API (``depends.core: v1``)."""

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_sessions(self, since: str | None = None) -> list[dict]:
        """Return session list items, newest-first as served, filtered by ``last_seen_at``.

        The checkpoint filter runs against the *list* payload's ``last_seen_at``
        (a confirmed field of ``GET /v1/sessions`` items), deliberately not the
        export payload, whose ``last_seen_at`` location is unverified. Items
        without the field are always included.
        """
        items: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = await self._client.get(f"{self._base_url}/v1/sessions", params=params)
            response.raise_for_status()
            payload = response.json()
            items.extend(item for item in payload.get("items", []) if isinstance(item, dict))
            cursor = payload.get("next_cursor")
            if not cursor:
                break

        if since is None:
            return items
        return [
            item
            for item in items
            if not item.get("last_seen_at") or _is_newer(item["last_seen_at"], since)
        ]

    async def export_session(self, session_id: str) -> dict:
        response = await self._client.get(f"{self._base_url}/v1/sessions/{session_id}/export")
        response.raise_for_status()
        return response.json()
