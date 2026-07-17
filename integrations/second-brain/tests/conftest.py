"""Test harness for the second-brain bot.

Everything here runs offline: no cognee server, no network, no API keys. The
router / identity / forget tests use the in-memory fake adapter; the cognee
adapter tests drive it with a fake HTTP client (see test_cognee_adapter.py). The
harness wires a Bot exactly as run.py would, but with the fake adapter and a
frozen clock so link-code TTLs are deterministic.
"""

from __future__ import annotations

import pytest
from cognee_integration_second_brain.consent import ConsentStore
from cognee_integration_second_brain.fake_adapter import FakeChatMemoryAdapter
from cognee_integration_second_brain.identity import IdentityStore, LinkingService
from cognee_integration_second_brain.interface import Conversation
from cognee_integration_second_brain.router import Bot


class BotHarness:
    """A fully wired bot over the fake adapter, plus helpers to simulate transports."""

    def __init__(self, default_opt_in: bool = True) -> None:
        self.adapter = FakeChatMemoryAdapter()
        self.identity = IdentityStore()
        # Frozen clock so issued link codes never expire mid-test.
        self.linking = LinkingService(self.identity, ttl_seconds=600, clock=lambda: 0.0)
        self.consent = ConsentStore(default_opt_in=default_opt_in)
        self.bot = Bot(self.adapter, self.identity, self.linking, self.consent)

    async def send(
        self,
        transport: str,
        external_user: str,
        text: str,
        *,
        source: str | None = None,
        ts: str = "2026-06-12T10:00:00",
        msg_ref: str | None = None,
    ) -> str:
        source = source or external_user
        msg_ref = msg_ref or f"{transport}://{source}/{ts}"
        conversation = Conversation(
            transport=transport,
            source=source,
            external_user=external_user,
            msg_ref=msg_ref,
        )
        return await self.bot.handle(conversation, text, ts)

    def conversation(
        self,
        transport: str,
        external_user: str,
        *,
        source: str | None = None,
        msg_ref: str | None = None,
    ) -> Conversation:
        """Build a fully resolved Conversation for direct adapter calls in tests."""
        source = source or external_user
        canonical = self.identity.resolve(transport, external_user)
        return Conversation(
            transport=transport,
            source=source,
            canonical_user=canonical,
            external_user=external_user,
            msg_ref=msg_ref or f"{transport}://{source}",
        )


@pytest.fixture
def harness() -> BotHarness:
    return BotHarness()
