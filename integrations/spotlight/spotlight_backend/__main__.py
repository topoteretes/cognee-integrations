"""Run the backend: ``python -m spotlight_backend`` (or ``uv run python -m spotlight_backend``)."""

import faulthandler
import signal

import uvicorn

from .config import Settings
from .server import create_app

# kill -USR1 <pid> dumps every thread's stack to stderr — first tool to reach
# for when a cognee store call hangs inside the serving process.
faulthandler.register(signal.SIGUSR1)


def main() -> None:
    settings = Settings.from_env()
    app = create_app(settings)
    print(
        f"Cognee Spotlight backend | mode={settings.mode} | http://{settings.host}:{settings.port}"
    )
    import os

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=os.getenv("SPOTLIGHT_LOG_LEVEL", "warning"),
    )


if __name__ == "__main__":
    main()
