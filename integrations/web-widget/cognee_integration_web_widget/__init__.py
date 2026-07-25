"""cognee-integration-web-widget — an embeddable web chat widget backed by cognee memory.

Talks to a running cognee server over HTTP; no in-process cognee needed.

Public API::

    from cognee_integration_web_widget import ChatMemoryAdapter, CogneeHttpClient
"""

from .adapter import Answer, ChatMemoryAdapter, Conversation
from .citations import Citation, split_evidence
from .http_client import CogneeHttpClient

__all__ = [
    "Answer",
    "ChatMemoryAdapter",
    "Conversation",
    "Citation",
    "split_evidence",
    "CogneeHttpClient",
]

__version__ = "0.1.0"
