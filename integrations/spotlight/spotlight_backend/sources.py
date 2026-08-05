"""Data sources: memory that fills itself.

A source is anything that can be synced into the index on a schedule. Three
ship here:

- :class:`LocalFolderSource` — the indexed roots re-sync automatically, so a
  file saved into a watched folder becomes searchable without touching the
  app (the indexer is incremental: unchanged files cost nothing).
- :class:`SlackSource` — pulls channel history via the Slack Web API into
  markdown transcripts (one file per channel per sync window), which then
  index like any document. Enabled by ``SLACK_TOKEN`` + ``SLACK_CHANNELS``.
- :class:`GoogleDriveSource` — pulls text-exportable Drive files via REST
  (``GDRIVE_ACCESS_TOKEN``, optional ``GDRIVE_QUERY``). Google Docs export as
  plain text; binary formats download as-is for cognee's loaders.

Connectors write into ``<data_dir>/sources/<name>/`` and let the ordinary
indexing pipeline do the rest, so every source inherits incrementality,
catalog mapping, and openable results for free. The sync loop runs inside
the backend; ``/sources`` reports status.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any, Optional


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-") or "item"


class LocalFolderSource:
    """The watched-folder source: whatever roots the catalog knows re-sync."""

    name = "folders"
    label = "Folders"
    icon = "folder"  # SF Symbol; the app renders whatever the backend reports

    def __init__(self, indexer: Any) -> None:
        self._indexer = indexer

    async def sync(self) -> str:
        if not self._indexer._catalog.roots:
            return "no folders indexed yet"
        started = self._indexer.start([])
        return "reindex started" if started else "indexer busy"


class SlackSource:
    """Channel history -> markdown transcripts -> the index.

    v1 pulls the most recent window per configured channel each sync; the
    file per (channel, day) is rewritten and the incremental indexer picks it
    up only when its content actually changed.
    """

    name = "slack"
    label = "Slack"
    icon = "bubble.left.and.bubble.right"

    def __init__(
        self, staging: Path, token: str, channels: list[str], *, client: Any = None
    ) -> None:
        self.staging = staging
        self.token = token
        self.channels = channels
        self._client = client  # injectable for tests

    async def sync(self) -> str:
        written = 0
        for channel in self.channels:
            messages = await self._history(channel)
            if not messages:
                continue
            by_day: dict[str, list[str]] = {}
            for message in messages:
                stamp = float(message.get("ts", 0) or 0)
                day = time.strftime("%Y-%m-%d", time.localtime(stamp))
                who = message.get("user") or message.get("username") or "someone"
                text = str(message.get("text", "")).strip()
                if text:
                    by_day.setdefault(day, []).append(f"- {who}: {text}")
            for day, lines in by_day.items():
                path = self.staging / f"slack-{_slug(channel)}-{day}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# Slack #{channel} — {day}\n\n" + "\n".join(lines) + "\n")
                written += 1
        return f"wrote {written} transcript file(s)"

    async def _history(self, channel: str) -> list[dict[str, Any]]:
        response = await self._get(
            "https://slack.com/api/conversations.history",
            params={"channel": channel, "limit": 200},
        )
        if response.status_code >= 400:
            return []
        data = response.json()
        return data.get("messages", []) if data.get("ok") else []

    async def _get(self, url: str, params: dict) -> Any:
        import httpx

        headers = {"Authorization": f"Bearer {self.token}"}
        if self._client is not None:
            return await self._client.request("GET", url, headers=headers, params=params)
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await client.get(url, headers=headers, params=params)


class GoogleDriveSource:
    """Drive files -> local staging -> the index.

    Uses the plain REST API with a bearer token (OAuth or service account —
    however the token was minted). Google-native docs export as text; other
    files download raw for cognee's loaders.
    """

    name = "gdrive"
    label = "Drive"
    icon = "externaldrive"

    def __init__(self, staging: Path, token: str, query: str = "", *, client: Any = None) -> None:
        self.staging = staging
        self.token = token
        self.query = query or "trashed = false"
        self._client = client

    async def sync(self) -> str:
        listing = await self._get(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": self.query, "pageSize": 50, "fields": "files(id,name,mimeType)"},
        )
        if listing.status_code >= 400:
            return f"listing failed (HTTP {listing.status_code})"
        files = listing.json().get("files", [])
        written = 0
        for f in files:
            file_id, name, mime = (
                f.get("id"),
                _slug(str(f.get("name", ""))),
                str(f.get("mimeType", "")),
            )
            if mime.startswith("application/vnd.google-apps"):
                content = await self._get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                    params={"mimeType": "text/plain"},
                )
                suffix = ".txt"
            else:
                content = await self._get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}",
                    params={"alt": "media"},
                )
                suffix = Path(str(f.get("name", ""))).suffix or ".bin"
            if content.status_code >= 400:
                continue
            path = self.staging / f"gdrive-{name}{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                content.content if hasattr(content, "content") else content.text.encode()
            )
            written += 1
        return f"synced {written} file(s)"

    async def _get(self, url: str, params: dict) -> Any:
        import httpx

        headers = {"Authorization": f"Bearer {self.token}"}
        if self._client is not None:
            return await self._client.request("GET", url, headers=headers, params=params)
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            return await client.get(url, headers=headers, params=params)


class MockConnectorSource:
    """A demo stand-in for a real connector: stages canned fictional content
    into ``<data_dir>/sources/<name>/`` so the app shows a live connection
    (and its documents index and search) without any external credentials.

    Enabled with ``SPOTLIGHT_MOCK_SOURCES=slack,gdrive``. Content is written
    once per file; the incremental indexer ignores unchanged files after that.
    """

    def __init__(
        self,
        name: str,
        staging: Path,
        files: dict[str, str],
        label: str = "",
        icon: str = "",
    ) -> None:
        self.name = name
        self.staging = staging
        self.files = files
        self.label = label or name.title()
        self.icon = icon or "puzzlepiece.extension"

    async def sync(self) -> str:
        written = 0
        for filename, content in self.files.items():
            path = self.staging / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.read_text() != content:
                path.write_text(content)
                written += 1
        return f"connected (demo) — {len(self.files)} document(s), {written} refreshed"


# Fictional Meridian Travel Group content for the mock connectors — same
# invented company as the demo corpus, so nothing real can leak.
MOCK_SLACK_FILES = {
    "slack-product-2026-08-04.md": (
        "# Slack #product — 2026-08-04\n\n"
        "- alex.chen: unified search beta is live for 5% of traffic — "
        "meaning-based queries convert 12% better than keyword so far\n"
        "- sam.rivera: RoomRover cut prices again in the São Paulo market, "
        "sales wants the counter-offer playbook updated by Friday\n"
        "- alex.chen: supplier API v3 rate limits bite above 40 rps — "
        "batching fix is in review, ships with Thursday's deploy\n"
        "- jamie.park: StayFinder's new loyalty tiers are getting press; "
        "our v2 needs the referral hook to stay competitive\n"
    ),
    "slack-eng-incidents-2026-08-03.md": (
        "# Slack #eng-incidents — 2026-08-03\n\n"
        "- sam.rivera: search latency spiked to 4s after the cache change — "
        "rolled back, root cause was per-query session rewrites\n"
        "- alex.chen: postmortem action: keep response caching off for "
        "search-as-you-type paths, warm the engine at boot instead\n"
        "- jamie.park: added the regression check to the deploy runbook\n"
    ),
}

MOCK_GDRIVE_FILES = {
    "gdrive-board-update-q3-draft.txt": (
        "Meridian Travel Group — Q3 Board Update (draft)\n\n"
        "Growth: bookings up 18% QoQ, driven by the unified search beta.\n"
        "Competition: RoomRover discounting aggressively in Brazil; "
        "Wanderly retreating to premium segments.\n"
        "Risks: supplier API v3 migration must land before peak season; "
        "loyalty v2 slips if referral hook misses September.\n"
    ),
    "gdrive-partner-pricing-tiers.txt": (
        "Meridian partner pricing tiers (internal)\n\n"
        "Tier 1 (chains, >500 properties): 8% commission, dedicated support.\n"
        "Tier 2 (regional groups): 11% commission, quarterly reviews.\n"
        "Tier 3 (independents): 14% commission, self-serve onboarding.\n"
        "Price-match escalations go through the sales playbook, not ad hoc.\n"
    ),
}


class SourceManager:
    """Runs every configured source on an interval and indexes what lands."""

    def __init__(self, indexer: Any, data_dir: Path, interval: float = 60.0) -> None:
        self.indexer = indexer
        self.data_dir = data_dir
        self.interval = interval
        self.sources: list[Any] = []
        self.status: dict[str, dict[str, Any]] = {}
        self._task: Optional[asyncio.Task] = None

    @classmethod
    def from_env(cls, indexer: Any, data_dir: Path) -> "SourceManager":
        manager = cls(indexer, data_dir, interval=float(os.getenv("SPOTLIGHT_SYNC_INTERVAL", "60")))
        if os.getenv("SPOTLIGHT_WATCH_FOLDERS", "true").lower() != "false":
            manager.sources.append(LocalFolderSource(indexer))
        if token := os.getenv("SLACK_TOKEN"):
            channels = [c.strip() for c in os.getenv("SLACK_CHANNELS", "").split(",") if c.strip()]
            if channels:
                manager.sources.append(SlackSource(data_dir / "sources" / "slack", token, channels))
        if token := os.getenv("GDRIVE_ACCESS_TOKEN"):
            manager.sources.append(
                GoogleDriveSource(
                    data_dir / "sources" / "gdrive", token, os.getenv("GDRIVE_QUERY", "")
                )
            )
        # demo connections: mock connectors for sources with no credentials,
        # skipped for any name already covered by a real connector above.
        # Each mock borrows the real connector's label/icon so it renders
        # identically in the app.
        mocks = {
            "slack": (MOCK_SLACK_FILES, SlackSource.label, SlackSource.icon),
            "gdrive": (MOCK_GDRIVE_FILES, GoogleDriveSource.label, GoogleDriveSource.icon),
        }
        configured = {s.name for s in manager.sources}
        for name in (x.strip().lower() for x in os.getenv("SPOTLIGHT_MOCK_SOURCES", "").split(",")):
            if name in mocks and name not in configured:
                files, label, icon = mocks[name]
                manager.sources.append(
                    MockConnectorSource(
                        name, data_dir / "sources" / name, files, label=label, icon=icon
                    )
                )
        return manager

    def start(self) -> None:
        if self.sources and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        # connectors first, so their staged files are on disk before the
        # folder source triggers the (single-flight) indexer
        while True:
            for source in self.sources:
                if isinstance(source, LocalFolderSource):
                    continue
                await self._run(source)
            staged = [
                str(s.staging) for s in self.sources if hasattr(s, "staging") and s.staging.exists()
            ]
            if staged:
                self.indexer.start(staged)
            for source in self.sources:
                if isinstance(source, LocalFolderSource):
                    await self._run(source)
            await asyncio.sleep(self.interval)

    async def _run(self, source: Any) -> None:
        try:
            detail = await source.sync()
            self.status[source.name] = {"ok": True, "detail": detail, "at": time.time()}
        except Exception as exc:
            traceback.print_exc()
            self.status[source.name] = {
                "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "at": time.time(),
            }
