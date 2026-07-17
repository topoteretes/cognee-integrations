"""cognee-integration-telegram — a Telegram bot where each chat is a cognee memory.

Talks to a running cognee server over HTTP; no in-process cognee needed.

Public API::

    from cognee_integration_telegram import CogneeMemoryAdapter, Settings, build_application
"""

from .adapter import Answer, CogneeMemoryAdapter
from .citations import CitationLedger, MessageRef
from .config import Settings
from .http_client import CogneeHttpClient
from .scoping import Scope, resolve_scope

__all__ = [
    "Answer",
    "CogneeMemoryAdapter",
    "CogneeHttpClient",
    "CitationLedger",
    "MessageRef",
    "Settings",
    "Scope",
    "resolve_scope",
    "build_application",
]

__version__ = "0.1.0"


def build_application(settings, adapter=None):
    """Lazily import the PTB-backed application builder.

    Kept lazy so importing the adapter/scoping/citations modules (and running
    their tests) does not require ``python-telegram-bot`` to be installed.
    """
    from .bot import build_application as _build

    return _build(settings, adapter)
