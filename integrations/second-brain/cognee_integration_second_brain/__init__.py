"""cognee-integration-second-brain — one memory graph, reachable from many transports.

Capture a note in Telegram, recall it from the web; both resolve to the same
private brain because the bot knows they are the same person (``/link`` merges
two transport identities onto one ``brain:{user}`` dataset). Cited recall,
``/forget me`` wipes everywhere.

Talks to a running cognee server over HTTP; no in-process cognee needed.

Public API::

    from cognee_integration_second_brain import Bot, CogneeChatMemoryAdapter, Settings
"""

from .cognee_adapter import CogneeChatMemoryAdapter, select_citations
from .config import Settings
from .consent import ConsentStore
from .fake_adapter import FakeChatMemoryAdapter
from .http_client import CogneeHttpClient
from .identity import IdentityStore, LinkingService
from .interface import (
    Answer,
    ChatMemoryAdapter,
    Citation,
    Conversation,
    Message,
    dataset_for,
    resolve_user,
)
from .router import Bot, classify, render_reply

__all__ = [
    "Answer",
    "Bot",
    "ChatMemoryAdapter",
    "Citation",
    "CogneeChatMemoryAdapter",
    "CogneeHttpClient",
    "ConsentStore",
    "Conversation",
    "FakeChatMemoryAdapter",
    "IdentityStore",
    "LinkingService",
    "Message",
    "Settings",
    "classify",
    "dataset_for",
    "render_reply",
    "resolve_user",
    "select_citations",
]

__version__ = "0.1.0"
