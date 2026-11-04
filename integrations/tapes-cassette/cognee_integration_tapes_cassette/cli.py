"""One-shot sync for cron and scripts: ``cognee-tapes-sync [--full] [--json]``.

Runs a single sync pass with the same ``Syncer`` the cassette service uses
(same transcript rules, state file and checkpoint), then exits. Point it at the
same ``CASSETTE_STATE_PATH`` as a running cassette: a run lock next to that file
keeps the two from syncing at once, and the second one reports ``busy``.

Exit codes: 0 completed, 1 failed, 2 busy (another sync is running).
"""

import argparse
import asyncio
import json
import logging
import os
import sys

from .config import load_config, load_env_file

EXIT_COMPLETED = 0
EXIT_FAILED = 1
EXIT_BUSY = 2


async def _sync_once(full: bool):
    # Imported here so .env is loaded before cognee reads its settings.
    from . import ingest
    from .tapes_client import TapesClient

    config = load_config()
    ingest.apply_storage_isolation(config)
    tapes = TapesClient(config.tapes_base_url)
    try:
        syncer = ingest.Syncer(config, tapes)
        syncer.start(full=full)
        return await syncer.wait()
    finally:
        await tapes.aclose()


def _summary(snapshot: dict) -> str:
    line = (
        f"{snapshot['state']}: fetched {snapshot['fetched']}, ingested {snapshot['ingested']}, "
        f"unchanged {snapshot['unchanged']}, skipped {snapshot['skipped']} "
        f"(dataset {snapshot['dataset']})"
    )
    return f"{line}: {snapshot['error']}" if snapshot.get("error") else line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cognee-tapes-sync",
        description="Sync recorded tapes sessions into cognee once, then exit.",
    )
    parser.add_argument("--full", action="store_true", help="ignore the checkpoint")
    parser.add_argument("--json", action="store_true", help="print the status as JSON")
    parser.add_argument(
        "--env-file", default=".env", help="settings file to load first (default: ./.env)"
    )
    args = parser.parse_args(argv)

    load_env_file(args.env_file)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "WARNING").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    snapshot = asyncio.run(_sync_once(args.full)).snapshot()
    print(json.dumps(snapshot) if args.json else _summary(snapshot))
    return {"completed": EXIT_COMPLETED, "busy": EXIT_BUSY}.get(snapshot["state"], EXIT_FAILED)


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
