"""Suite descriptors for the two near-identical Python integrations.

claude-code and codex are the same hook code differing only in constants. A Suite
captures those differences so one parametrized test set runs against both.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# .../integrations/tests/utils/suites.py -> parents[2] == .../integrations
_INTEGRATIONS = Path(__file__).resolve().parents[2]

#: Name of the plugin config dir under HOME, shared by both suites.
PLUGIN_DIR_NAME = ".cognee-plugin"


@dataclass(frozen=True)
class Suite:
    """A single integration suite (claude-code or codex)."""

    name: str
    scripts_dir: Path
    #: Subdirectory under ~/.cognee-plugin used for state ("" for claude, "codex" for codex).
    state_subdir: str
    default_dataset: str
    agent_name: str
    session_prefix: str
    #: Env var the scripts read for the working directory.
    cwd_env: str
    agent_email: str
    session_suffix: str


CLAUDE = Suite(
    name="claude-code",
    scripts_dir=_INTEGRATIONS / "claude-code" / "scripts",
    state_subdir="",
    default_dataset="claude_sessions",
    agent_name="claude-code-agent",
    session_prefix="cc",
    cwd_env="CLAUDE_CWD",
    agent_email="claude-code@cognee.agent",
    session_suffix="_claude",
)

CODEX = Suite(
    name="codex",
    scripts_dir=_INTEGRATIONS / "codex" / "plugins" / "cognee" / "scripts",
    state_subdir="codex",
    default_dataset="codex_sessions",
    agent_name="codex-agent",
    session_prefix="codex",
    cwd_env="CODEX_CWD",
    agent_email="codex@cognee.agent",
    session_suffix="_codex",
)

ALL_SUITES = [CLAUDE, CODEX]


def config_dir(home: Path | str) -> Path:
    """The ~/.cognee-plugin config dir under the given (temp) HOME.

    The config file (config.json) and the server-ready marker live here for both
    suites.
    """
    return Path(home) / PLUGIN_DIR_NAME


def state_dir(suite: Suite, home: Path | str) -> Path:
    """The suite's state/plugin dir under the given (temp) HOME.

    claude-code uses ~/.cognee-plugin directly; codex nests under
    ~/.cognee-plugin/codex.
    """
    base = config_dir(home)
    return base / suite.state_subdir if suite.state_subdir else base
