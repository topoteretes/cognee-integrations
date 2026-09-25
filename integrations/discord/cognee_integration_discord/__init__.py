"""Cognee-backed memory for Discord servers (per-guild/per-channel isolation).

Public surface:
    - ChatMemoryAdapter / CogneeHttpAdapter — the memory seam (see #3608)
    - MemoryService — platform-agnostic bot behavior
    - build_cog / run — discord.py wiring
"""

from .adapter import ChatMemoryAdapter, CogneeHttpAdapter, RecallResult
from .service import AnswerResult, MemoryService

__all__ = [
    "ChatMemoryAdapter",
    "CogneeHttpAdapter",
    "RecallResult",
    "MemoryService",
    "AnswerResult",
]
