"""Shared chat-memory adapter core for cognee-powered bots.

A small, framework-agnostic layer that every cognee bot (Slack, Telegram,
Discord, a personal second brain, and so on) plugs into, so each bot stays thin
and they all share one memory model built on cognee's ``remember`` / ``recall`` /
``forget`` primitives.

Quick start::

    from cognee_integration_chat_memory import (
        ChatMemoryAdapter, Conversation, Message, per_channel_scope,
    )

    # Talks to a running cognee server (COGNEE_BASE_URL / COGNEE_API_KEY).
    adapter = ChatMemoryAdapter(scope=per_channel_scope)

    convo = Conversation(platform="slack", workspace="T1", channel="C1", user="U1")
    await adapter.ingest(convo, Message(text="We ship on Friday.", user="U1"))
    answer = await adapter.answer(convo, "when do we ship?")
    print(answer.text, [c.permalink for c in answer.citations])

The default :class:`CogneeHttpMemoryBackend` needs no in-process cognee. Inject
:class:`InMemoryMemoryBackend` for keyless tests/demos, or install the ``[sdk]``
extra to use :class:`CogneeMemoryBackend` (in-process cognee SDK). The full
"build your own bot in 5 minutes" guide lives in this package's ``README.md``.
"""

from .adapter import ChatMemoryAdapter
from .backend import (
    CogneeHttpMemoryBackend,
    CogneeMemoryBackend,
    InMemoryMemoryBackend,
    MemoryBackend,
)
from .consent import ConsentStore, InMemoryConsentStore
from .models import Answer, Citation, Conversation, Message, Scope
from .sanitizer import sanitize_key
from .scoping import per_channel_scope, per_user_scope

__all__ = [
    "ChatMemoryAdapter",
    "MemoryBackend",
    "CogneeHttpMemoryBackend",
    "CogneeMemoryBackend",
    "InMemoryMemoryBackend",
    "ConsentStore",
    "InMemoryConsentStore",
    "Conversation",
    "Message",
    "Scope",
    "Answer",
    "Citation",
    "sanitize_key",
    "per_channel_scope",
    "per_user_scope",
]
