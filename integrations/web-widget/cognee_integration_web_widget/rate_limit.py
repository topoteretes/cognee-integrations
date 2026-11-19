"""Request limits for the public chat endpoints.

``/api/chat`` and ``/api/forget`` need no login, and every chat message runs a
cognee recall billed to the tenant. Two guards keep that bounded:

* **Per visitor:** one client IP gets at most ``WIDGET_RATE_LIMIT_PER_IP_HOUR``
  requests an hour and ``WIDGET_RATE_LIMIT_PER_IP_DAY`` a day, across both
  endpoints. It is keyed on the IP rather than ``visitor_id`` or
  ``conversation_id`` because the client makes those up.
* **Global:** all visitors together get at most ``WIDGET_RATE_LIMIT_GLOBAL_HOUR``
  chat requests an hour. It is the only limit that bounds spend when the
  requests come from many IPs.

``0`` turns a limit off. The counters live in this process, which is enough for
the single instance the backend runs as (see ``DEPLOYMENT-REQUIREMENTS.md``).

Behind a reverse proxy every request arrives from the proxy's address, so
``WIDGET_TRUSTED_PROXIES`` names the proxies whose ``X-Forwarded-For`` is
believed: a comma-separated list of IPs or CIDR ranges, or ``*`` to believe
whichever peer connects. Unset, the header is ignored, since anyone can send it.
"""

from __future__ import annotations

import ipaddress
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional, Union

HOUR = 3600.0
DAY = 86400.0

DEFAULT_PER_IP_HOUR = 20
DEFAULT_PER_IP_DAY = 100
DEFAULT_GLOBAL_HOUR = 500

# Every this many checks, drop the counters of IPs that have gone quiet, so a
# stream of one-off visitors does not grow the table for ever.
SWEEP_EVERY = 1000


def env_limit(name: str, default: int) -> int:
    """Read one limit from the environment.

    Unset or blank means the default. Anything but a non-negative whole number
    raises, so a typo stops the backend at startup instead of quietly running it
    without the limit.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise ValueError(
            f"{name} must be a whole number of requests (0 turns the limit off), got {raw!r}"
        )
    return value


class Window:
    """At most ``limit`` hits per key within any ``seconds``-long stretch."""

    def __init__(self, limit: int, seconds: float):
        self.limit = limit
        self.seconds = seconds
        self._hits: dict[str, deque[float]] = {}

    def _live(self, key: str, now: float) -> Optional[deque[float]]:
        hits = self._hits.get(key)
        if hits is None:
            return None
        while hits and hits[0] <= now - self.seconds:
            hits.popleft()
        if not hits:
            del self._hits[key]
            return None
        return hits

    def retry_after(self, key: str, now: float) -> Optional[float]:
        """Seconds until ``key`` may go again, or ``None`` if it may go now."""
        if not self.limit:
            return None
        hits = self._live(key, now)
        if hits is None or len(hits) < self.limit:
            return None
        return hits[0] + self.seconds - now

    def record(self, key: str, now: float) -> None:
        if self.limit:
            self._hits.setdefault(key, deque()).append(now)

    def sweep(self, now: float) -> None:
        for key in list(self._hits):
            self._live(key, now)


@dataclass(frozen=True)
class Limited:
    """A refused request: which guard refused it and when to try again."""

    scope: str  # "visitor" or "global"
    retry_after: int  # whole seconds, at least 1


class RateLimits:
    """The per-visitor and global guards, checked together."""

    def __init__(
        self,
        per_ip_hour: int = DEFAULT_PER_IP_HOUR,
        per_ip_day: int = DEFAULT_PER_IP_DAY,
        global_hour: int = DEFAULT_GLOBAL_HOUR,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ip_hour = Window(per_ip_hour, HOUR)
        self.ip_day = Window(per_ip_day, DAY)
        self.global_hour = Window(global_hour, HOUR)
        self.clock = clock
        self._checks = 0

    @classmethod
    def from_env(cls) -> "RateLimits":
        return cls(
            per_ip_hour=env_limit("WIDGET_RATE_LIMIT_PER_IP_HOUR", DEFAULT_PER_IP_HOUR),
            per_ip_day=env_limit("WIDGET_RATE_LIMIT_PER_IP_DAY", DEFAULT_PER_IP_DAY),
            global_hour=env_limit("WIDGET_RATE_LIMIT_GLOBAL_HOUR", DEFAULT_GLOBAL_HOUR),
        )

    def check(self, ip: str, *, counts_globally: bool) -> Optional[Limited]:
        """Count one request from ``ip``, or refuse it.

        A refused request is not counted, so a visitor who keeps retrying while
        limited is let back in when the window passes, not pushed further out.
        There is no ``await`` between checking and recording, so concurrent
        requests on the event loop cannot both slip under a limit.
        """
        now = self.clock()
        guards = [(self.ip_hour, ip, "visitor"), (self.ip_day, ip, "visitor")]
        if counts_globally:
            guards.append((self.global_hour, "*", "global"))
        for window, key, scope in guards:
            wait = window.retry_after(key, now)
            if wait is not None:
                return Limited(scope=scope, retry_after=max(1, math.ceil(wait)))
        for window, key, _ in guards:
            window.record(key, now)

        self._checks += 1
        if self._checks % SWEEP_EVERY == 0:
            self.ip_hour.sweep(now)
            self.ip_day.sweep(now)
        return None


TrustedProxies = Union[str, list]  # "*" or a list of ip networks


def parse_trusted_proxies(raw: str) -> Optional[TrustedProxies]:
    """``WIDGET_TRUSTED_PROXIES`` as ``None`` (trust none), ``"*"``, or networks."""
    raw = raw.strip()
    if not raw:
        return None
    if raw == "*":
        return "*"
    networks = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise ValueError(
                f"WIDGET_TRUSTED_PROXIES must be '*' or comma-separated IPs or CIDR ranges, "
                f"got {part!r}"
            ) from None
    return networks or None


def _is_trusted(address: str, trusted: list) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in trusted)


def client_ip(peer: str, forwarded_for: str, trusted: Optional[TrustedProxies]) -> str:
    """The address to count a request against.

    ``peer`` is who opened the connection. Only when that peer is a trusted
    proxy is ``X-Forwarded-For`` read, and then from the right: each proxy
    appends the address it saw, so the rightmost hop that is not itself a
    trusted proxy is the visitor. Hops further left were written by the client
    and could say anything.
    """
    if trusted is None:
        return peer
    if trusted != "*" and not _is_trusted(peer, trusted):
        return peer
    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
    if not hops:
        return peer
    if trusted == "*":
        return hops[-1]
    for hop in reversed(hops):
        if not _is_trusted(hop, trusted):
            return hop
    return hops[0]
