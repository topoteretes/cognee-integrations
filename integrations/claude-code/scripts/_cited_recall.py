"""Cited recall (SDK-1099): whether the server writes memory tags, and its refusals.

A cognee server that lists ``recall_cited_v1`` on ``/health`` answers
``result_mode="cited"`` on ``POST /api/v1/recall``: the regular ``only_context``
prompt with a tag line, ``[mem:<kind>/<dataset_id>/<record_id>@<version>]``,
directly before every passage, entity and fact, and those tags in
``metadata["records"]``. The hook asks for it only when the server's cached
``/health`` lists the capability, so any other server gets today's request.

Stdlib only; the plugin modules it needs are imported where they are used.
"""

from __future__ import annotations

import time
import urllib.error

CAPABILITY = "recall_cited_v1"
#: The cached ``/health`` answer, written by the context-cache refresh (cloud)
#: and by session start (a local server).
CAPABILITIES_FILE = "server-capabilities.json"


def capabilities_path():
    from _cloud_worker import root

    return root() / CAPABILITIES_FILE


def record_capabilities(health: dict) -> None:
    """Cache the ``version`` and ``capabilities`` a ``/health`` answer lists,
    for the server the plugin targets now."""
    from _cloud_worker import target
    from _injection_ledger import atomic_json

    atomic_json(
        capabilities_path(),
        {
            "version": str(health.get("version") or ""),
            "capabilities": list(health.get("capabilities") or []),
            "target": target(),
            "checked_at": time.time(),
        },
    )


def supported(base_url: str) -> bool:
    """True when the cached ``/health`` of ``base_url`` lists ``recall_cited_v1``."""
    from _cloud_worker import read

    data = read(capabilities_path())
    target = data.get("target")
    cached = str(target.get("base_url") or "") if isinstance(target, dict) else ""
    capabilities = data.get("capabilities")
    return (
        bool(base_url)
        and cached.strip().rstrip("/") == str(base_url).strip().rstrip("/")
        and isinstance(capabilities, list)
        and CAPABILITY in capabilities
    )


def refused(exc: BaseException) -> bool:
    """True when the server refused a cited recall by name: a 422 whose detail
    names a ``Cited…`` error, e.g. a searched dataset without passages. The
    caller then sends the regular recall instead."""
    if not isinstance(exc, urllib.error.HTTPError) or exc.code != 422:
        return False
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        return False
    return "[Cited" in body
