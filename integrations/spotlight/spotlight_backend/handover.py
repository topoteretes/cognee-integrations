"""Team handover: pass distilled learnings from one person's memory to another's.

Modeled on the four-layer memory picture (org > team > user > agent) and built
with the exact primitives the cognee Claude Code plugin already uses -- a
central cognee server reached over HTTP, ``/api/v1/remember`` writes tagged
with a ``node_set``, and datasets as the sharing boundary:

    handover-inbox-<user>   direct handover to one person
    team-<team>-memory      shared with one team
    org-memory              shared with everyone

A handover note is a small markdown document (title, from, scope, body,
source). Sending = remembering it into the right dataset on the central
server; receiving = polling those datasets for new items, then ingesting each
new learning into the recipient's *local* searchable memory, so a handover
shows up in their ⌥Space like any of their own documents.

Failure semantics follow the Claude Code plugin's contract: a connection
failure is "unreachable" (retry later), an HTTP error is authoritative (do
not retry blindly, surface it). All calls go through one injectable client so
tests run against a fake server.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

HANDOVER_NODE_SET = "handover"
ORG_DATASET = "org-memory"


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return value or "unknown"


@dataclass
class HandoverConfig:
    """Who I am and where the central server lives.

    Env names deliberately overlap the Claude Code plugin's (``COGNEE_BASE_URL``
    / ``COGNEE_API_KEY``), so a machine already set up for agent-session memory
    points at the same central server with zero extra config.
    """

    user: str = ""
    team: str = ""
    base_url: str = ""
    api_key: str = ""

    @classmethod
    def from_env(cls) -> "HandoverConfig":
        return cls(
            user=os.getenv("SPOTLIGHT_USER", "").strip(),
            team=os.getenv("SPOTLIGHT_TEAM", "").strip(),
            base_url=(os.getenv("COGNEE_HUB_URL") or os.getenv("COGNEE_BASE_URL") or "").rstrip(
                "/"
            ),
            api_key=os.getenv("COGNEE_HUB_API_KEY") or os.getenv("COGNEE_API_KEY") or "",
        )

    @property
    def enabled(self) -> bool:
        return bool(self.user and self.base_url)


@dataclass
class HandoverItem:
    id: str
    dataset_id: str
    name: str
    layer: str  # inbox | team | org
    created_at: str
    seen: bool
    body: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dataset_id": self.dataset_id,
            "name": self.name,
            "layer": self.layer,
            "created_at": self.created_at,
            "seen": self.seen,
            "body": self.body,
        }


@dataclass
class HandoverService:
    config: HandoverConfig
    data_dir: Path
    adapter: Any = None  # local cognee adapter; received learnings are ingested here
    client: Any = None  # injectable HTTP client for tests
    _seen: dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            self._seen = json.loads(self._seen_path.read_text())
        except (OSError, ValueError):
            self._seen = {}

    # -- datasets ------------------------------------------------------------
    @property
    def inbox_dataset(self) -> str:
        return f"handover-inbox-{slugify(self.config.user)}"

    @property
    def team_dataset(self) -> str:
        return f"team-{slugify(self.config.team)}-memory" if self.config.team else ""

    def dataset_for_recipient(self, to: str) -> str:
        """``org`` broadcasts, ``team:<name>`` shares with a team, anything
        else is a person's inbox."""
        to = to.strip()
        if to.lower() == "org":
            return ORG_DATASET
        if to.lower().startswith("team:"):
            return f"team-{slugify(to[5:])}-memory"
        return f"handover-inbox-{slugify(to)}"

    def _layer_of(self, dataset_name: str) -> str:
        if dataset_name == ORG_DATASET:
            return "org"
        if dataset_name.startswith("team-"):
            return "team"
        return "inbox"

    # -- sending ---------------------------------------------------------------
    async def share(self, to: str, title: str, body: str, source: str = "") -> dict[str, Any]:
        """Compose a handover note and remember it into the recipient's dataset."""
        dataset = self.dataset_for_recipient(to)
        # A local single-user engine enforces no permissions (access control
        # is off in that posture) — writing into another user's inbox there
        # would be an unchecked cross-user write. Only a real hub, where the
        # server checks the API key's grants, may deliver to other people.
        from cognee_backend_core.adapters import LocalCogneeAdapter

        if isinstance(self.adapter, LocalCogneeAdapter) and dataset != self.inbox_dataset:
            return {
                "ok": False,
                "error": "sharing with others needs a central hub (cloud mode); "
                "the local engine has no permission enforcement",
            }
        date = time.strftime("%Y-%m-%d %H:%M")
        note = (
            f"# Handover: {title}\n\n"
            f"- from: {self.config.user}\n"
            f"- to: {to}\n"
            f"- date: {date}\n"
            + (f"- source: {source}\n" if source else "")
            + f"\n{body.strip()}\n"
        )
        stamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{stamp}--from-{slugify(self.config.user)}--{slugify(title)}.md"
        response = await self._request(
            "POST",
            "/api/v1/remember",
            data={
                "datasetName": dataset,
                "node_set": HANDOVER_NODE_SET,
                "run_in_background": "true",
            },
            files=[("data", (filename, note.encode("utf-8"), "text/markdown"))],
        )
        response.raise_for_status()
        return {"ok": True, "dataset": dataset, "note": filename}

    # -- receiving -------------------------------------------------------------
    async def inbox(self, include_bodies: bool = True) -> dict[str, Any]:
        """Everything shared with me across my layers, newest first.

        New (unseen) items are ingested into local memory as a side effect, so
        by the time the app shows the notification the learning is already
        searchable.
        """
        my_datasets = {self.inbox_dataset, self.team_dataset, ORG_DATASET} - {""}
        listing = await self._datasets()
        items: list[HandoverItem] = []
        for ds in listing:
            name = str(ds.get("name", ""))
            if name not in my_datasets:
                continue
            ds_id = str(ds.get("id", ""))
            for data in await self._dataset_data(ds_id):
                item_id = str(data.get("id", ""))
                items.append(
                    HandoverItem(
                        id=item_id,
                        dataset_id=ds_id,
                        name=str(data.get("name", "")),
                        layer=self._layer_of(name),
                        created_at=str(data.get("created_at", "")),
                        seen=bool(self._seen.get(item_id)),
                    )
                )
        items.sort(key=lambda i: i.created_at, reverse=True)
        for item in items:
            if include_bodies and (not item.seen or len(items) <= 20):
                item.body = await self._item_raw(item.dataset_id, item.id)
        await self._ingest_new(items)
        return {
            "items": [i.to_dict() for i in items],
            "unseen": sum(1 for i in items if not i.seen),
        }

    def mark_seen(self, ids: list[str]) -> None:
        for item_id in ids:
            self._seen[item_id] = True
        self._seen_path.parent.mkdir(parents=True, exist_ok=True)
        self._seen_path.write_text(json.dumps(self._seen))

    async def _ingest_new(self, items: list[HandoverItem]) -> None:
        """Write unseen learnings to disk and add them to local memory, so a
        received handover is findable via normal search immediately."""
        new = [i for i in items if not i.seen and i.body and not self._ingested(i.id)]
        if not new or self.adapter is None:
            return
        paths = []
        for item in new:
            path = self._handover_dir / f"{slugify(item.name) or item.id}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(item.body)
            self._mark_ingested(item.id)
            paths.append(str(path))
        try:
            await self.adapter.add(paths)
            await self.adapter.cognify()
        except Exception:
            pass  # local ingest is best-effort; the note is still in the inbox

    # -- local state ----------------------------------------------------------
    @property
    def _seen_path(self) -> Path:
        return self.data_dir / "handover_seen.json"

    @property
    def _handover_dir(self) -> Path:
        return self.data_dir / "handovers"

    def _ingested(self, item_id: str) -> bool:
        return (self._handover_dir / f".ingested-{item_id}").exists()

    def _mark_ingested(self, item_id: str) -> None:
        self._handover_dir.mkdir(parents=True, exist_ok=True)
        (self._handover_dir / f".ingested-{item_id}").touch()

    # -- server calls -----------------------------------------------------------
    async def _datasets(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/v1/datasets")
        if response.status_code >= 400:
            return []
        data = response.json()
        return data if isinstance(data, list) else []

    async def _dataset_data(self, dataset_id: str) -> list[dict[str, Any]]:
        response = await self._request("GET", f"/api/v1/datasets/{dataset_id}/data")
        if response.status_code >= 400:
            return []
        data = response.json()
        return data if isinstance(data, list) else []

    async def _item_raw(self, dataset_id: str, data_id: str) -> str:
        try:
            response = await self._request(
                "GET", f"/api/v1/datasets/{dataset_id}/data/{data_id}/raw"
            )
            if response.status_code >= 400:
                return ""
            return response.text
        except Exception:
            return ""

    async def _request(self, method: str, path: str, **kwargs):
        import httpx

        headers = {"X-Api-Key": self.config.api_key} if self.config.api_key else {}
        headers.update(kwargs.pop("headers", {}))
        url = self.config.base_url + path
        if self.client is not None:
            return await self.client.request(method, url, headers=headers, **kwargs)
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            return await client.request(method, url, headers=headers, **kwargs)


def make_handover(settings, adapter: Any) -> Optional[HandoverService]:
    config = HandoverConfig.from_env()
    if not config.enabled:
        return None
    return HandoverService(config=config, data_dir=settings.data_dir, adapter=adapter)
