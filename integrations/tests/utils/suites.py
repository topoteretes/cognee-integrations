"""Suite descriptors for the two near-identical Python integrations.

claude-code and codex are the same hook code differing only in constants. A Suite
captures those differences so one parametrized test set runs against both.

Constants are verified against each suite's ``config.py`` / ``_plugin_common.py``:
  - claude-code: config AND state both live in ``~/.cognee-plugin/claude-code/``
  - codex:       config.json lives at the shared root ``~/.cognee-plugin/``,
                 state nests under ``~/.cognee-plugin/codex/``
  - both:        default dataset ``agent_sessions``; the shared server-ready
                 marker sits at the ``~/.cognee-plugin/`` root; local-SDK data
                 dirs live under ``~/.cognee/``
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# .../integrations/tests/utils/suites.py -> parents[2] == .../integrations
_INTEGRATIONS = Path(__file__).resolve().parents[2]

#: Name of the shared plugin root under HOME, used by both suites.
PLUGIN_DIR_NAME = ".cognee-plugin"

#: Local-SDK home (data/system/cache dirs and the .env file) under HOME.
COGNEE_HOME_DIR_NAME = ".cognee"


@dataclass(frozen=True)
class Suite:
    """A single integration suite (claude-code or codex)."""

    name: str
    scripts_dir: Path
    #: Subdirectory under ~/.cognee-plugin holding config.json ("" = the root).
    config_subdir: str
    #: Subdirectory under ~/.cognee-plugin holding per-suite state.
    state_subdir: str
    default_dataset: str
    agent_name: str
    session_prefix: str
    #: Env var the scripts read for the working directory.
    cwd_env: str
    #: Suffix _resolve_agent_name appends to the agent session name.
    session_suffix: str


CLAUDE = Suite(
    name="claude-code",
    scripts_dir=_INTEGRATIONS / "claude-code" / "scripts",
    config_subdir="claude-code",
    state_subdir="claude-code",
    default_dataset="agent_sessions",
    agent_name="claude-code-agent",
    session_prefix="claude",
    cwd_env="CLAUDE_CWD",
    session_suffix="_claude",
)

CODEX = Suite(
    name="codex",
    scripts_dir=_INTEGRATIONS / "codex" / "plugins" / "cognee" / "scripts",
    config_subdir="",
    state_subdir="codex",
    default_dataset="agent_sessions",
    agent_name="codex-agent",
    session_prefix="codex",
    cwd_env="CODEX_CWD",
    session_suffix="_codex",
)

ALL_SUITES = [CLAUDE, CODEX]


def plugin_root(home: Path | str) -> Path:
    """The shared ~/.cognee-plugin root under the given (temp) HOME.

    The server-ready marker and (for codex) config.json live here.
    """
    return Path(home) / PLUGIN_DIR_NAME


def config_dir(suite: Suite, home: Path | str) -> Path:
    """The dir holding the suite's config.json under the given (temp) HOME."""
    base = plugin_root(home)
    return base / suite.config_subdir if suite.config_subdir else base


def state_dir(suite: Suite, home: Path | str) -> Path:
    """The suite's state/plugin dir under the given (temp) HOME."""
    base = plugin_root(home)
    return base / suite.state_subdir if suite.state_subdir else base


def cognee_home(home: Path | str) -> Path:
    """The ~/.cognee dir (local-SDK data/system/cache, .env) under a temp HOME."""
    return Path(home) / COGNEE_HOME_DIR_NAME
