import os
from dataclasses import dataclass
from pathlib import Path

CASSETTE_NAME = "cognee"
CASSETTE_VERSION = "0.1.0"

DEFAULT_TAPES_BASE_URL = "http://127.0.0.1:8081"
DEFAULT_DATASET = "tapes_sessions"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9900


@dataclass(frozen=True)
class Config:
    tapes_base_url: str
    dataset_name: str
    host: str
    port: int
    state_path: Path
    # When set, cognee's data/system roots are forced under this directory so
    # the cassette never writes into a globally-configured cognee storage
    # location (e.g. storage env vars exported from a shell profile).
    storage_root: str | None


def load_config() -> Config:
    dataset_name = os.environ.get("COGNEE_TAPES_DATASET", DEFAULT_DATASET)
    return Config(
        tapes_base_url=os.environ.get("TAPES_BASE_URL", DEFAULT_TAPES_BASE_URL).rstrip("/"),
        dataset_name=dataset_name,
        host=os.environ.get("CASSETTE_HOST", DEFAULT_HOST),
        port=int(os.environ.get("CASSETTE_PORT", str(DEFAULT_PORT))),
        state_path=Path(
            os.environ.get("CASSETTE_STATE_PATH", f".cognee-cassette-state-{dataset_name}.json")
        ),
        storage_root=os.environ.get("COGNEE_STORAGE_ROOT") or None,
    )
