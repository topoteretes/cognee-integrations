"""Suite descriptors for the near-identical Python hook integrations.

Claude Code, Codex, and Antigravity share a hook runtime with host-specific
adapters and constants. A Suite describes those differences so the same tests
exercise every registered integration.

Constants are verified against each suite's ``config.py`` / ``_plugin_common.py``:
  - claude-code: state lives in ``~/.cognee-plugin/claude-code/``
  - codex:       state nests under ``~/.cognee-plugin/codex/``
  - antigravity: state nests under ``~/.cognee-plugin/antigravity/``
  - all:        default dataset ``agent_sessions``; the shared server-ready
                 marker sits at the ``~/.cognee-plugin/`` root; local-SDK data
                 dirs live under ``~/.cognee/``
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# .../integrations/tests/utils/suites.py -> parents[2] == .../integrations
_INTEGRATIONS = Path(__file__).resolve().parents[2]

#: Name of the shared plugin root under HOME, used by all registered suites.
PLUGIN_DIR_NAME = ".cognee-plugin"

#: Local-SDK home (data/system/cache dirs and the .env file) under HOME.
COGNEE_HOME_DIR_NAME = ".cognee"


@dataclass(frozen=True)
class Suite:
    """A single host integration suite."""

    name: str
    scripts_dir: Path
    #: Subdirectory under ~/.cognee-plugin holding per-suite state.
    state_subdir: str
    default_dataset: str
    agent_name: str
    session_prefix: str
    #: The suite's hooks.json manifest (claude: <root>/hooks/, codex: plugin root).
    hooks_json: Path
    #: How hooks.json groups registrations: host events or named hooks.
    hook_manifest_style: str
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
    #: Capability: the host runs ``async`` hooks and emits ``StopFailure``, so
    #: credits can refresh at turn end without adding a prompt of lag. codex skips
    #: async hooks entirely and has no StopFailure, so its entry must be a plain
    #: sync Stop hook with a tight timeout.
    has_async_hooks: bool
    #: Capability: renders a rich terminal status bar — the health glyphs, the
    #: recall-counts diagnostics strip, the mode word and the plugin-install
    #: registry. codex instead emits a short plain-text line injected into the
    #: model's context, so those segment-level assertions do not apply to it, though
    #: its bar still has to render and name the dataset.
    #:
    #: Not a blanket "codex has no segments": tests for individual segments probe
    #: for their own symbol, so whatever codex does ship is picked up on its own.
    has_rich_statusline: bool
    #: Capability: ``pre-compact.py`` produces an anchor against a server.
    #:
    #: True for every suite since the local-SDK path was removed (PR #405). The
    #: seed recall still passes an empty query (there is no user question at
    #: compact time) and the server matches nothing on it, but claude-code's hook
    #: now falls back to the session *detail* endpoint, which returns the recent
    #: QA and trace rows without a query — so the anchor is built over HTTP on
    #: both backends. Kept as a flag rather than an assumption: a future
    #: integration could arrive without that fallback, and the live test would
    #: then say so instead of asserting an anchor it cannot produce.
    has_precompact_http: bool
    #: Capability (SDK-594): one improve submit per trigger. The improve path takes
    #: no machine-wide per-session lock, never re-submits a busy answer, has no
    #: post-submit pipeline-status poll (``wait_for_cognify`` is gone), records
    #: failed attempts so the idle/auto cooldown arms as a ``backoff``, exposes
    #: ``run_session_improve_detailed`` -> ``{"ok", "reason", "error"}`` in place
    #: of the boolean ``run_session_improve``, the final sync defers a busy answer
    #: instead of retrying it, and the idle watcher exits after one attempt and
    #: runs no "shutdown" improve when stopped. Antigravity still carries the
    #: lock, the 15s busy loop, the poll and the shutdown flush.
    has_single_submit_improve: bool
    #: Capability: ``session-context-lookup.py`` still carries an in-process
    #: local-SDK recall branch (``cognee.recall`` awaited directly when no service
    #: URL is configured) next to the HTTP one. claude-code and codex dropped it
    #: (PR #405 / "delete old local SDK mode"); Antigravity keeps it, so its
    #: concurrent fan-out has two dispatch paths to pin, not one.
    has_local_sdk_recall: bool
    #: Capability: the cross-dataset search flow — ``list_readable_datasets`` /
    #: ``cached_readable_datasets`` in the common module, ``list-datasets.py``,
    #: the prompt hook's "Other Cognee datasets you can search" hint on every
    #: answered prompt, and ``cognee-search.sh --dataset-id`` forcing a foreign dataset to a
    #: graph-only read. claude-code and codex carry it; Antigravity does not.
    has_cross_dataset_search: bool
    #: Capability: the recap skills (``cognee-standup`` / ``cognee-digest`` /
    #: ``cognee-timeline``) and the ``cognee-recap.py`` wrapper behind them.
    #: claude-code and codex ship the same script (the doctor hint follows the
    #: tree: ``cognee-doctor.sh`` vs ``doctor.py``); Antigravity does not.
    has_recap_skills: bool
    #: Capability (SDK-1071): recalled memory is tagged for crediting, and the
    #: resume notice shown, without ``COGNEE_PLUGIN_CITE_MEMORY`` being set.
    #: claude-code is on; codex starts off, because its terminal shows the
    #: injected instruction, until it has been tried live. Antigravity has no
    #: credit tagging at all.
    cites_memory_by_default: bool
    #: Behaviour: a prompt whose recall found nothing still injects the memory
    #: header and ``(no memory matches for this prompt)``. codex injects nothing
    #: then (SDK-1071: a "0 hits" line on most turns read as "Cognee does
    #: nothing"); the user still sees the header.
    injects_empty_recall: bool
    #: Behaviour (SDK-1090): against a remote tenant the hooks only spool to disk
    #: and one detached per-user worker (``_cloud_worker.py``) does every server
    #: call. SessionStart registers the launch with it and returns without picking
    #: an endpoint, and the graph bridge is the worker's ``improve_fired``, not the
    #: local path's ``sync_bridge_done`` / ``final_sync_once_*`` events. codex and
    #: Antigravity still talk to the tenant from the hooks.
    has_cloud_worker: bool


CLAUDE = Suite(
    name="claude-code",
    hook_manifest_style="event-map",
    scripts_dir=_INTEGRATIONS / "claude-code" / "scripts",
    hooks_json=_INTEGRATIONS / "claude-code" / "hooks" / "hooks.json",
    plugin_manifest=_INTEGRATIONS / "claude-code" / ".claude-plugin" / "plugin.json",
    state_subdir="claude-code",
    default_dataset="agent_sessions",
    agent_name="claude-code-agent",
    session_prefix="claude",
    cwd_env="CLAUDE_CWD",
    session_suffix="_claude",
    host_stem="claude",
    has_async_hooks=True,
    has_rich_statusline=True,
    has_precompact_http=True,
    has_single_submit_improve=True,
    has_local_sdk_recall=False,
    has_cross_dataset_search=True,
    has_recap_skills=True,
    cites_memory_by_default=True,
    injects_empty_recall=True,
    has_cloud_worker=True,
)

CODEX = Suite(
    name="codex",
    hook_manifest_style="event-map",
    scripts_dir=_INTEGRATIONS / "codex" / "plugins" / "cognee" / "scripts",
    hooks_json=_INTEGRATIONS / "codex" / "plugins" / "cognee" / "hooks.json",
    plugin_manifest=_INTEGRATIONS
    / "codex"
    / "plugins"
    / "cognee"
    / ".codex-plugin"
    / "plugin.json",
    state_subdir="codex",
    default_dataset="agent_sessions",
    agent_name="codex-agent",
    session_prefix="codex",
    cwd_env="CODEX_CWD",
    session_suffix="_codex",
    host_stem="codex",
    has_async_hooks=False,
    has_rich_statusline=False,
    has_precompact_http=True,
    has_single_submit_improve=True,
    has_local_sdk_recall=False,
    has_cross_dataset_search=True,
    has_recap_skills=True,
    cites_memory_by_default=False,
    injects_empty_recall=False,
    has_cloud_worker=False,
)

ANTIGRAVITY = Suite(
    name="antigravity",
    scripts_dir=_INTEGRATIONS / "antigravity" / "scripts",
    hooks_json=_INTEGRATIONS / "antigravity" / "hooks.json",
    plugin_manifest=_INTEGRATIONS / "antigravity" / "plugin.json",
    state_subdir="antigravity",
    default_dataset="agent_sessions",
    agent_name="antigravity-agent",
    session_prefix="antigravity",
    cwd_env="AGY_CWD",
    session_suffix="_agy",
    host_stem="agy",
    has_async_hooks=False,
    has_rich_statusline=False,
    has_precompact_http=True,
    hook_manifest_style="named",
    has_single_submit_improve=False,
    has_local_sdk_recall=True,
    has_cross_dataset_search=False,
    has_recap_skills=False,
    cites_memory_by_default=False,
    injects_empty_recall=True,
    has_cloud_worker=False,
)

#: Cursor runs a copy of the codex runtime behind ``cursor_hook.py``, the adapter
#: that turns Cursor's native hook payloads into the Claude Code contract the
#: shared scripts read. The shared tests drive those scripts directly, so they
#: hold the copy to the codex behaviour; the adapter and the Cursor-format
#: ``hooks.json`` have their own contract tests (``test_cursor_*.py``).
CURSOR = Suite(
    name="cursor",
    # Cursor's native format: event -> [{"command", "timeout"}], no matcher groups.
    hook_manifest_style="cursor",
    scripts_dir=_INTEGRATIONS / "cursor" / "scripts",
    hooks_json=_INTEGRATIONS / "cursor" / "hooks" / "hooks.json",
    plugin_manifest=_INTEGRATIONS / "cursor" / ".cursor-plugin" / "plugin.json",
    state_subdir="cursor",
    default_dataset="agent_sessions",
    agent_name="cursor-agent",
    session_prefix="cursor",
    cwd_env="CURSOR_CWD",
    session_suffix="_cursor",
    host_stem="cursor",
    has_async_hooks=False,
    has_rich_statusline=False,
    has_precompact_http=True,
    has_single_submit_improve=True,
    has_local_sdk_recall=False,
    has_cross_dataset_search=True,
    has_recap_skills=False,
    cites_memory_by_default=False,
    injects_empty_recall=False,
    has_cloud_worker=False,
)

ALL_SUITES = [CLAUDE, CODEX, ANTIGRAVITY, CURSOR]


def plugin_root(home: Path | str) -> Path:
    """The shared ~/.cognee-plugin root under the given (temp) HOME.

    The server-ready marker lives here.
    """
    return Path(home) / PLUGIN_DIR_NAME


def state_dir(suite: Suite, home: Path | str) -> Path:
    """The suite's state/plugin dir under the given (temp) HOME."""
    base = plugin_root(home)
    return base / suite.state_subdir if suite.state_subdir else base


def cognee_home(home: Path | str) -> Path:
    """The ~/.cognee dir (local-SDK data/system/cache, .env) under a temp HOME."""
    return Path(home) / COGNEE_HOME_DIR_NAME
