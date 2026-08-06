"""Runtime settings for the second-brain bot, read from the environment.

The bot talks to a running cognee server over HTTP, so it holds no LLM key of
its own (that lives on the server). Nothing here is required for the fake-adapter
first run; ``COGNEE_BASE_URL`` / ``COGNEE_API_KEY`` point the real adapter at a
cognee server, and ``TELEGRAM_BOT_TOKEN`` enables the Telegram transport.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() == "true"


@dataclass(frozen=True)
class Settings:
    """Bot configuration, assembled from environment variables."""

    cognee_base_url: str = "http://localhost:8000"
    cognee_api_key: str = ""
    telegram_bot_token: str = ""
    # Loopback by default: the bot runs without auth, so binding to all interfaces
    # would expose a private brain to the network. Set WEB_HOST=0.0.0.0 to expose it.
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    require_optin: bool = False
    use_fake_adapter: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            cognee_base_url=os.environ.get("COGNEE_BASE_URL", "http://localhost:8000"),
            cognee_api_key=os.environ.get("COGNEE_API_KEY", ""),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            web_host=os.environ.get("WEB_HOST", "127.0.0.1"),
            web_port=int(os.environ.get("WEB_PORT", "8080")),
            require_optin=_env_bool("REQUIRE_OPTIN"),
            use_fake_adapter=_env_bool("USE_FAKE_ADAPTER"),
        )
