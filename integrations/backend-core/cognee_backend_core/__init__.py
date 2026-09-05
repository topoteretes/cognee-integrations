"""Standardized cognee access layer for integrations in this monorepo."""

from .adapters import FakeAdapter, HttpCogneeAdapter, LocalCogneeAdapter
from .results import best_text, chunk_text, extract_file_hint, first_text, unwrap_results
from .runtime import single_user_runtime

__all__ = [
    "FakeAdapter",
    "HttpCogneeAdapter",
    "LocalCogneeAdapter",
    "best_text",
    "chunk_text",
    "extract_file_hint",
    "first_text",
    "single_user_runtime",
    "unwrap_results",
]
