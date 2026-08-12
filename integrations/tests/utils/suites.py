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
    #: The suite's hooks.json manifest (claude: <root>/hooks/, codex: plugin root).
    hooks_json: Path
    #: The plugin manifest whose "version" the runtime reports as its own.
    plugin_manifest: Path
    #: Env var the scripts read for the working directory.
    cwd_env: str
    #: Suffix _resolve_agent_name appends to the agent session name.
    session_suffix: str
    #: Base name of the host CLI's own executable, as _proc's Windows ancestry
    #: walk matches it (``claude.exe`` / ``codex.exe``, and ``<stem>-*`` variants
    #: like ``claude-nightly.exe``). Deliberately separate from session_prefix:
    #: the two happen to share a value, but one names a process and the other a
    #: session, and nothing keeps them equal.
    host_stem: str
    #: Capability: has the background-remember + cognify-poll refactor. Submits
    #: writes with run_in_background=true, returns an {"ok": ...} envelope from
    #: _post_remember_document instead of raising, exposes
    #: _plugin_common.wait_for_cognify, and honours the bounded wait in
    #: _remember_http. The improve path has its own flag below — that part of the
    #: refactor did not travel with the rest.
    #:
    #: True for BOTH suites as of the port that landed in main: codex previously
    #: had the older synchronous, raise-on-error path, which meant one document's
    #: HTTP error aborted its sibling. Kept as a flag rather than deleted because
    #: it names a real contract that a future integration may not satisfy.
    has_background_remember: bool
    #: Capability: ``improve_session_via_http`` polls the cognify and memify
    #: pipelines and reports ``cognify_status``/``memify_status``. Split out from
    #: has_background_remember because the port that landed in main covered the
    #: bridge, ``wait_for_cognify`` and the bounded ``do_remember`` wait, but NOT
    #: the improve path — codex has no ``cognify_status`` anywhere.
    has_improve_pipeline_polling: bool
    #: Capability: the host runs ``async`` hooks and emits ``StopFailure``, so
    #: credits can refresh at turn end without adding a prompt of lag. codex skips
    #: async hooks entirely and has no StopFailure, so its entry must be a plain
    #: sync Stop hook with a tight timeout.
    has_async_hooks: bool
    #: Capability: exposes the shared ``_plugin_common.elapsed_ms`` helper (#3676),
    #: and logs it on the bridge's ``http_bridge_poll`` / failed-submit events.
    has_elapsed_ms_helper: bool
    #: Capability: logs an *aggregate* ``elapsed_ms`` on the ``context_lookup_*``
    #: events. Split from the helper flag because codex now has the helper but
    #: still times only each recall scope inline — so ``per_scope[*]["elapsed_ms"]``
    #: is present on both suites while the per-prompt total is claude-code only.
    has_recall_latency_metric: bool
    #: Capability: renders a rich terminal status bar — the health glyphs, the
    #: recall-counts diagnostics strip, the mode word and the plugin-install
    #: registry. codex instead emits a short plain-text line injected into the
    #: model's context, so those segment-level assertions do not apply to it, though
    #: its bar still has to render and name the dataset.
    #:
    #: Not a blanket "codex has no segments": codex *does* have
    #: ``_pipeline_health_glyph`` as of the same port. Tests for individual segments
    #: probe for their own symbol, so they pick that up on their own.
    has_rich_statusline: bool
    #: Capability: ``pre-compact.py`` branches on ``is_cloud_mode`` and recalls via
    #: ``recall_via_http``. The one place codex is AHEAD: claude-code's pre-compact
    #: is local-SDK only, so it produces no anchor at all in server mode. Since
    #: ``is_cloud_mode`` is just ``bool(base_url)``, a loopback server counts, and
    #: codex takes the HTTP path everywhere the live tier runs.
    has_precompact_http: bool


CLAUDE = Suite(
    name="claude-code",
    scripts_dir=_INTEGRATIONS / "claude-code" / "scripts",
    hooks_json=_INTEGRATIONS / "claude-code" / "hooks" / "hooks.json",
    plugin_manifest=_INTEGRATIONS / "claude-code" / ".claude-plugin" / "plugin.json",
    config_subdir="claude-code",
    state_subdir="claude-code",
    default_dataset="agent_sessions",
    agent_name="claude-code-agent",
    session_prefix="claude",
    cwd_env="CLAUDE_CWD",
    session_suffix="_claude",
    host_stem="claude",
    has_background_remember=True,
    has_improve_pipeline_polling=True,
    has_async_hooks=True,
    has_elapsed_ms_helper=True,
    has_recall_latency_metric=True,
    has_rich_statusline=True,
    has_precompact_http=False,
)

CODEX = Suite(
    name="codex",
    scripts_dir=_INTEGRATIONS / "codex" / "plugins" / "cognee" / "scripts",
    hooks_json=_INTEGRATIONS / "codex" / "plugins" / "cognee" / "hooks.json",
    plugin_manifest=_INTEGRATIONS
    / "codex"
    / "plugins"
    / "cognee"
    / ".codex-plugin"
    / "plugin.json",
    config_subdir="",
    state_subdir="codex",
    default_dataset="agent_sessions",
    agent_name="codex-agent",
    session_prefix="codex",
    cwd_env="CODEX_CWD",
    session_suffix="_codex",
    host_stem="codex",
    has_background_remember=True,
    has_improve_pipeline_polling=False,
    has_async_hooks=False,
    has_elapsed_ms_helper=True,
    has_recall_latency_metric=False,
    has_rich_statusline=False,
    has_precompact_http=True,
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
