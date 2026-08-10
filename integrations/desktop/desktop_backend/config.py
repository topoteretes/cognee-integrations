"""Environment-driven settings for the Cognee desktop backend.

Three modes, chosen with ``COGNEE_MODE``:

- ``local`` -- import cognee in-process. Needs ``LLM_API_KEY`` in the
  environment (cognee extracts knowledge with an LLM at index time).
- ``cloud`` -- talk to a cognee server / cognee cloud over HTTP. Needs
  ``COGNEE_CLOUD_URL`` (or a local server URL) and usually ``COGNEE_CLOUD_API_KEY``.
- ``fake`` -- in-memory adapter, no keys and no cognee install. Substring
  search instead of semantic; exists so the whole app can be tried and
  tested end to end offline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_EXTENSIONS = {
    ".md",
    ".txt",
    ".rst",
    ".org",
    ".pdf",
    ".docx",
    ".pptx",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".swift",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".sh",
    ".html",
    ".css",
}

# Directories that are never worth indexing (build output, VCS, caches).
SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".build",
    "build",
    "dist",
    "target",
    ".cache",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "DerivedData",
    ".Trash",
}


def env(name: str, default: str = "") -> str:
    """A COGNEE_DESKTOP_* variable, honoring the legacy SPOTLIGHT_* spelling
    from installs configured before the product rename."""
    return os.getenv(f"COGNEE_DESKTOP_{name}", os.getenv(f"SPOTLIGHT_{name}", default))


def _default_data_dir() -> Path:
    """``~/.cognee-desktop``, unless a pre-rename ``~/.cognee-spotlight``
    already holds state — then keep using it, so nothing needs migrating."""
    new = Path.home() / ".cognee-desktop"
    legacy = Path.home() / ".cognee-spotlight"
    if not new.exists() and legacy.exists():
        return legacy
    return new


@dataclass
class Settings:
    mode: str = "fake"  # local | cloud | fake
    host: str = "127.0.0.1"
    port: int = 8765
    dataset: str = "main"
    cloud_base_url: str = "https://api.cognee.ai"
    cloud_api_key: str = ""
    user: str = ""  # this backend's identity (COGNEE_DESKTOP_USER)
    search_scope: str = "all"  # cloud mode: "all" tenant datasets | "dataset" (just ours)
    exclude_datasets: set[str] = field(default_factory=set)  # kept out of "all" searches
    experiments: bool = False  # latent features (feedback, contradictions, temporal)
    data_dir: Path = field(default_factory=_default_data_dir)
    max_file_size: int = 5 * 1024 * 1024  # skip files larger than 5 MB
    extensions: set[str] = field(default_factory=lambda: set(DEFAULT_EXTENSIONS))

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.mode = os.getenv("COGNEE_MODE", s.mode).strip().lower()
        s.host = env("HOST", s.host)
        s.port = int(env("PORT", str(s.port)))
        s.dataset = env("DATASET", s.dataset)
        s.cloud_base_url = os.getenv("COGNEE_CLOUD_URL", s.cloud_base_url).rstrip("/")
        s.cloud_api_key = os.getenv("COGNEE_CLOUD_API_KEY", s.cloud_api_key)
        s.user = env("USER", s.user).strip()
        s.search_scope = env("SEARCH_SCOPE", s.search_scope).strip().lower()
        if excluded := env("EXCLUDE_DATASETS"):
            s.exclude_datasets = {x.strip() for x in excluded.split(",") if x.strip()}
        s.experiments = env("EXPERIMENTS").lower() in {"1", "true", "yes"}
        if data_dir := env("DATA_DIR"):
            s.data_dir = Path(data_dir)
        if exts := env("EXTENSIONS"):
            s.extensions = {
                e if e.startswith(".") else f".{e}"
                for e in (x.strip().lower() for x in exts.split(","))
                if e
            }
        if s.mode not in {"local", "cloud", "fake"}:
            raise ValueError(f"COGNEE_MODE must be local, cloud or fake, got {s.mode!r}")
        return s
