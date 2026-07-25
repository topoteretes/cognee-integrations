"""Single-command runner for the second-brain bot.

    python -m cognee_integration_second_brain
    # or, after install:  cognee-second-brain

Reads configuration from the environment (see ``.env.example`` / config.py):

    COGNEE_BASE_URL     the running cognee server the bot stores/queries against
    COGNEE_API_KEY      set only if that server has access control enabled
    TELEGRAM_BOT_TOKEN  enables the Telegram transport when set
    WEB_HOST / WEB_PORT web transport bind address (default 127.0.0.1:8080)
    REQUIRE_OPTIN       "true" to require /optin before capturing
    USE_FAKE_ADAPTER    "true" to run the in-memory adapter with no cognee server

The web transport always runs; Telegram runs too when a token is present, so
"2+ transports" works out of the box once you add a token.
"""

from __future__ import annotations

import asyncio

from .config import Settings
from .consent import ConsentStore
from .fake_adapter import FakeChatMemoryAdapter
from .http_client import CogneeHttpClient
from .identity import IdentityStore, LinkingService
from .router import Bot


def build_bot(settings: Settings) -> Bot:
    if settings.use_fake_adapter:
        adapter = FakeChatMemoryAdapter()
    else:
        from .cognee_adapter import CogneeChatMemoryAdapter

        adapter = CogneeChatMemoryAdapter(
            client=CogneeHttpClient(settings.cognee_base_url, settings.cognee_api_key)
        )
    identity = IdentityStore()
    linking = LinkingService(identity)
    consent = ConsentStore(default_opt_in=not settings.require_optin)
    return Bot(adapter, identity, linking, consent)


async def _serve(settings: Settings) -> None:
    import uvicorn

    from .telegram_transport import TelegramTransport
    from .web_transport import build_web_app

    bot = build_bot(settings)
    app = build_web_app(bot)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.web_host, port=settings.web_port, log_level="info")
    )

    tasks = [server.serve()]
    print(f"Web transport listening on http://{settings.web_host}:{settings.web_port}/message")
    if settings.telegram_bot_token:
        tasks.append(TelegramTransport(bot, settings.telegram_bot_token).run())
        print("Telegram transport enabled.")
    else:
        print("TELEGRAM_BOT_TOKEN not set; running the web transport only.")

    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(_serve(Settings.from_env()))


if __name__ == "__main__":
    main()
