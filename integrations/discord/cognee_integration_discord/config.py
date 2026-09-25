"""Environment-based configuration for the Discord memory bot (HTTP variant)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class BotConfig:
    discord_token: str
    cognee_base_url: str = "http://localhost:8000"
    cognee_api_key: str = ""
    top_k: int = 5

    @classmethod
    def from_env(cls) -> "BotConfig":
        token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN is required to run the Discord memory bot.")
        return cls(
            discord_token=token,
            cognee_base_url=os.environ.get("COGNEE_BASE_URL", "http://localhost:8000"),
            cognee_api_key=os.environ.get("COGNEE_API_KEY", ""),
            top_k=_int_env("COGNEE_DISCORD_TOP_K", 5),
        )


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default
