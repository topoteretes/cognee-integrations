"""Shared test infrastructure for the Cognee host-hook integrations.

Building blocks (see README.md):
  - suites        : Host descriptors that parametrize tests over every available suite
  - identity_fake : stateful login / api-key / agent / dataset fake
  - mock_cognee   : lightweight mock Cognee HTTP server built on pytest-httpserver
  - payloads      : synthetic stdin hook-payload builders (all hook types)
  - isolation     : temp-HOME state isolation for subprocess (e2e) and in-process (unit) tests
  - fixtures      : pytest fixtures wiring the above together

Nothing here writes to the real ~/.cognee-plugin or ~/.cognee — all state is
redirected into a per-test temporary HOME.
"""

from .suites import (
    ALL_SUITES,
    ANTIGRAVITY,
    CLAUDE,
    CODEX,
    Suite,
    cognee_home,
    config_dir,
    plugin_root,
    state_dir,
)

__all__ = [
    "ALL_SUITES",
    "ANTIGRAVITY",
    "CLAUDE",
    "CODEX",
    "Suite",
    "cognee_home",
    "config_dir",
    "plugin_root",
    "state_dir",
]
