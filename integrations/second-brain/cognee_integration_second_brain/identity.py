"""Cross-transport identity: the ownable core of this bot.

A link table maps an external identity ``(transport, external_user)`` to one
``canonical_user_id``. First contact auto-creates a canonical user; ``/link``
points a second identity at an existing one, so two front-ends share one brain.

Canonical ids are deterministic (uuid5) from the first external identity, which
keeps tests reproducible and gives clean forget semantics: after wiping and
unlinking, messaging again yields a fresh empty brain, not the old one.

The one-time-code linking flow lives here too: ``/link`` on transport A issues a
short-lived code tied to A's canonical user; entering that code on transport B
points B's identity at A's canonical user. Codes are single-use and expire, and
the clock is injectable so TTL behavior is deterministic in tests.

In-memory by default; swap the dicts for real tables in production.
"""

from __future__ import annotations

import secrets
import time
import uuid
from typing import Callable, Optional

# Fixed namespace so canonical ids are stable across runs and processes.
_CANONICAL_NAMESPACE = uuid.UUID("6f3b9c2a-1d4e-5a6b-8c7d-9e0f1a2b3c4d")


class IdentityStore:
    def __init__(self) -> None:
        # (transport, external_user) -> canonical_user_id
        self._links: dict[tuple[str, str], str] = {}

    def resolve(self, transport: str, external_user: str) -> str:
        """Resolve an external identity to its canonical user, creating one on first contact."""
        key = (transport, external_user)
        if key not in self._links:
            self._links[key] = self._mint_canonical(transport, external_user)
        return self._links[key]

    def link(self, transport: str, external_user: str, canonical_user_id: str) -> None:
        """Point an external identity at an existing canonical user (merge front-ends)."""
        self._links[(transport, external_user)] = canonical_user_id

    def identities_for(self, canonical_user_id: str) -> list[tuple[str, str]]:
        """Every external identity currently linked to this canonical user."""
        return [key for key, value in self._links.items() if value == canonical_user_id]

    def unlink_all(self, canonical_user_id: str) -> None:
        """Drop every external identity link for a canonical user (used by forget)."""
        for key in self.identities_for(canonical_user_id):
            del self._links[key]

    @staticmethod
    def _mint_canonical(transport: str, external_user: str) -> str:
        return str(uuid.uuid5(_CANONICAL_NAMESPACE, f"{transport}:{external_user}"))


class LinkingService:
    def __init__(
        self,
        identity_store: IdentityStore,
        ttl_seconds: int = 600,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._store = identity_store
        self._ttl = ttl_seconds
        self._clock = clock or time.time
        # code -> (canonical_user_id, expires_at)
        self._codes: dict[str, tuple[str, float]] = {}

    def issue_code(self, canonical_user_id: str) -> str:
        """Issue a one-time link code for the given canonical user."""
        code = self._mint_code()
        self._codes[code] = (canonical_user_id, self._clock() + self._ttl)
        return code

    def redeem_code(self, code: str, transport: str, external_user: str) -> Optional[str]:
        """Redeem a code from transport B, linking it to the issuer's brain.

        Returns the canonical user id on success, or None if the code is
        unknown or expired.
        """
        # Minted codes are lowercase hex; normalize so a code retyped with a
        # different case (e.g. mobile autocapitalization) still redeems.
        code = code.strip().lower()
        entry = self._codes.get(code)
        if entry is None:
            return None

        canonical_user_id, expires_at = entry
        if self._clock() > expires_at:
            del self._codes[code]
            return None

        self._store.link(transport, external_user, canonical_user_id)
        del self._codes[code]  # one-time use
        return canonical_user_id

    @staticmethod
    def _mint_code() -> str:
        # Six lowercase hex chars: short enough to type, unguessable enough for a TTL window.
        return secrets.token_hex(3)
