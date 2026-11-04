"""Sync single-flight, atomic state writes, and clean shutdown.

A ``wait=True`` sync used to call ``Syncer.run`` directly, so ``is_running()``
never saw it and a second ``/api/sync`` could start another run against the
same state file. Every run now goes through the one tracked task.
"""

import asyncio
import json

import httpx
import pytest
from cognee_integration_tapes_cassette import ingest
from cognee_integration_tapes_cassette.server import create_app
from cognee_integration_tapes_cassette.tapes_client import TapesClient

from .conftest import SESSION_COMPLETED, make_tapes_transport, read_state


class BlockingCognee:
    """A fake cognee whose ``add`` waits until the test releases it."""

    def __init__(self):
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.added = []
        self.cognified = []

    async def add(self, data, dataset_name):
        self.entered.set()
        await self.release.wait()
        self.added.append((data, dataset_name))

    async def cognify(self, datasets):
        self.cognified.append(datasets)


@pytest.fixture
def blocking_cognee(monkeypatch):
    fake = BlockingCognee()
    monkeypatch.setattr(ingest, "cognee", fake)
    return fake


def _app(config):
    tapes = TapesClient(
        config.tapes_base_url, client=httpx.AsyncClient(transport=make_tapes_transport())
    )
    return create_app(config, tapes=tapes)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cassette")


async def test_a_waited_sync_blocks_a_concurrent_one(config, blocking_cognee):
    app = _app(config)
    async with _client(app) as client:
        first = asyncio.create_task(client.post("/api/sync", json={"wait": True}))
        await asyncio.wait_for(blocking_cognee.entered.wait(), timeout=5)

        # Bounded: if a second run were started, it would block on the fake
        # ``add`` and these would time out instead of hanging the suite.
        second = await asyncio.wait_for(client.post("/api/sync", json={"wait": True}), 2)
        background = await asyncio.wait_for(client.post("/api/sync", json={}), 2)
        assert second.json()["accepted"] is False
        assert background.json()["accepted"] is False
        assert second.json()["status"]["state"] == "running"

        blocking_cognee.release.set()
        body = (await asyncio.wait_for(first, timeout=5)).json()

    assert body["accepted"] is True
    assert body["status"]["state"] == "completed"
    assert len(blocking_cognee.added) == 1  # one run, not two
    assert blocking_cognee.cognified == [[config.dataset_name]]


async def test_a_new_sync_is_accepted_once_the_previous_one_finished(config, blocking_cognee):
    blocking_cognee.release.set()
    app = _app(config)
    async with _client(app) as client:
        assert (await client.post("/api/sync", json={"wait": True})).json()["accepted"] is True
        again = (await client.post("/api/sync", json={"wait": True, "full": True})).json()
    assert again["accepted"] is True
    assert again["status"]["state"] == "completed"


def test_state_write_is_atomic(config, fake_cognee, monkeypatch):
    """A failed write leaves the previous state intact and no temp file behind."""
    syncer = ingest.Syncer(config, tapes=None)
    syncer._save_state(ingest._State(sessions={"a": "hash-1"}, last_synced_at="2026-08-20"))

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ingest.os, "replace", fail)
    with pytest.raises(OSError):
        syncer._save_state(ingest._State(sessions={"a": "hash-2"}))

    assert read_state(config)["sessions"] == {"a": "hash-1"}
    leftovers = [p for p in config.state_path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_state_round_trips(config, fake_cognee):
    syncer = ingest.Syncer(config, tapes=None)
    state = ingest._State(
        sessions={SESSION_COMPLETED["session"]["id"]: "h"},
        last_synced_at="2026-08-20T11:00:00+00:00",
        pending_cognify=True,
    )
    syncer._save_state(state)
    loaded = syncer._load_state()
    assert loaded == state
    assert json.loads(config.state_path.read_text())["pending_cognify"] is True


async def test_shutdown_cancels_a_running_sync(config, blocking_cognee):
    syncer = ingest.Syncer(
        config,
        TapesClient(
            config.tapes_base_url, client=httpx.AsyncClient(transport=make_tapes_transport())
        ),
    )
    assert syncer.start() is True
    await asyncio.wait_for(blocking_cognee.entered.wait(), timeout=5)

    await syncer.shutdown()

    assert not syncer.is_running()
    assert syncer.status.state == "cancelled"
    assert syncer.status.finished_at is not None
    assert blocking_cognee.added == []
