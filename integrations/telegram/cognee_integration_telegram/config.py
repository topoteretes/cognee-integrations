"""Runtime settings for the Telegram bot, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Bot configuration.

    ``bot_token`` is the only required value. ``cognee_base_url`` /
    ``cognee_api_key`` point the bot at a running cognee server (that server
    holds the ``LLM_API_KEY`` and builds/queries memory — the bot does not read
    it). A local cognee with access control disabled needs no api key.
    """

    bot_token: str
    cognee_base_url: str = "http://localhost:8000"
    cognee_api_key: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather, then "
                "export TELEGRAM_BOT_TOKEN=<token>. See the README for the 5-minute setup."
            )
        return cls(
            bot_token=token,
            cognee_base_url=os.environ.get("COGNEE_BASE_URL", "http://localhost:8000"),
            cognee_api_key=os.environ.get("COGNEE_API_KEY", ""),
        )
