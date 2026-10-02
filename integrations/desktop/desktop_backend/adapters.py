"""Cognee adapters for the Cognee desktop backend.

Thin layer over the shared ``cognee-backend-core`` package
(``integrations/backend-core``), which owns the adapter contract (local /
HTTP / fake), the result-shape helpers, and the hardened single-user runtime
posture. This module just picks the adapter for the configured mode.
"""

from __future__ import annotations

from cognee_backend_core import (
    FakeAdapter,
    HttpCogneeAdapter,
    LocalCogneeAdapter,
    chunk_text,
    extract_file_hint,
    first_text,
    unwrap_results,
)

# Historical names used across this integration and its tests.
CloudCogneeAdapter = HttpCogneeAdapter
_first_text = first_text


def make_adapter(settings):
    if settings.mode == "local":
        # Same scope contract as cloud: answers span every local dataset —
        # connector datasets (github-<repo>, staged sources) and the inbox
        # included — so the integrations tie together in local mode too.
        own_inbox = f"handover-inbox-{settings.user.lower()}" if settings.user else ""
        return LocalCogneeAdapter(
            settings.dataset,
            search_all=settings.search_scope != "dataset",
            exclude_datasets=settings.exclude_datasets,
            exclude_predicate=lambda name: name.startswith("handover-inbox-") and name != own_inbox,
        )
    if settings.mode == "cloud":
        # Search the whole tenant by default: someone connecting their cloud
        # tenant expects to find the data already living there, not just what
        # this app writes into its own dataset. Other people's handover
        # inboxes stay out of scope — their mail is not this user's memory
        # (and under real ACLs would not be readable at all).
        from cognee_backend_core import HttpCogneeAdapter as _Http

        own_inbox = f"handover-inbox-{settings.user.lower()}" if settings.user else ""
        return _Http(
            settings.dataset,
            settings.cloud_base_url,
            settings.cloud_api_key,
            search_all=settings.search_scope != "dataset",
            exclude_datasets=settings.exclude_datasets,
            exclude_predicate=lambda name: name.startswith("handover-inbox-") and name != own_inbox,
        )
    return FakeAdapter(settings.dataset)


__all__ = [
    "CloudCogneeAdapter",
    "FakeAdapter",
    "HttpCogneeAdapter",
    "LocalCogneeAdapter",
    "chunk_text",
    "extract_file_hint",
    "first_text",
    "make_adapter",
    "unwrap_results",
]
