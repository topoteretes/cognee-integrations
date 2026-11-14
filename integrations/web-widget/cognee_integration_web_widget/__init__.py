"""cognee-integration-web-widget — an embeddable web chat widget backed by cognee memory.

Talks to a running cognee server over HTTP; no in-process cognee needed.

Public API::

    from cognee_integration_web_widget import ChatMemoryAdapter, CogneeHttpClient
"""

import os

from dotenv import load_dotenv

# Settings are read from the environment when the modules below are imported,
# so the .env file has to be loaded first. It is looked for in the working
# directory (where `cp .env.example .env` puts it); WIDGET_ENV_FILE points at
# another file, or turns this off when empty. Variables already set in the
# environment win over the file.
_env_file = os.getenv("WIDGET_ENV_FILE", ".env")
if _env_file:
    load_dotenv(_env_file, override=False)

from .adapter import Answer, ChatMemoryAdapter, Conversation  # noqa: E402
from .citations import Citation, split_evidence  # noqa: E402
from .http_client import CogneeHttpClient  # noqa: E402

__all__ = [
    "Answer",
    "ChatMemoryAdapter",
    "Conversation",
    "Citation",
    "split_evidence",
    "CogneeHttpClient",
]

__version__ = "0.1.0"
