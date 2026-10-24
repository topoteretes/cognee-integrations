"""Cognee memory adapter for Browser Use agents."""

from .config import CogneeBrowserMemoryConfig, ConfigError, MemoryScope, resolve_config
from .http_backend import CogneeError, CogneeHttpBackend
from .memory import STATIC_INSTRUCTION, CogneeBrowserMemory, MemoryRun
from .models import (
    MemoryRecord,
    MemoryRunStatus,
    RecallContext,
    RecallHit,
    Receipt,
    ReceiptStatus,
    RecordKind,
    Verification,
)
from .receipts import ReceiptStore
from .sanitize import sanitize_url, sanitize_urls
from .tools import MEMORY_TOOL_NAMES, register_memory_tools

__version__ = "0.1.0"
VERIFIED_COGNEE_VERSION = "1.6.5"

__all__ = [
    "CogneeBrowserMemory",
    "CogneeBrowserMemoryConfig",
    "CogneeError",
    "CogneeHttpBackend",
    "ConfigError",
    "MEMORY_TOOL_NAMES",
    "MemoryRecord",
    "MemoryRun",
    "MemoryRunStatus",
    "MemoryScope",
    "RecallContext",
    "RecallHit",
    "Receipt",
    "ReceiptStatus",
    "ReceiptStore",
    "RecordKind",
    "STATIC_INSTRUCTION",
    "VERIFIED_COGNEE_VERSION",
    "Verification",
    "__version__",
    "register_memory_tools",
    "resolve_config",
    "sanitize_url",
    "sanitize_urls",
]
