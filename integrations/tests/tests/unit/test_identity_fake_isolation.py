"""The mock identity server refuses keys minted by another instance.

Detached plugin processes (the exit watcher, the deferred SessionEnd sync) can
outlive the test that started them and reach the next test's server on a fixed
port. A key minted by the earlier server must not let them create the
principal's dataset there: that flipped the next SessionStart's shared-memory
wiring to ``tenantless_with_data`` and made the host-session e2e test flaky.
"""

from __future__ import annotations

from utils.identity_fake import IdentityFake


def _minted_key(fake: IdentityFake) -> str:
    fake.seed_user("default_user@example.com")
    return fake.seed_owner_key("default_user@example.com")


def test_a_key_from_another_server_cannot_create_datasets():
    stale = _minted_key(IdentityFake())
    status, _ = IdentityFake().datasets_create("agent_sessions", stale)
    assert status == 401


def test_this_servers_own_key_still_works():
    fake = IdentityFake()
    status, row = fake.datasets_create("agent_sessions", _minted_key(fake))
    assert status == 201 and row["name"] == "agent_sessions"


def test_hand_written_test_keys_keep_their_permissive_treatment():
    status, _ = IdentityFake().datasets_create("agent_sessions", "test-key")
    assert status == 201
