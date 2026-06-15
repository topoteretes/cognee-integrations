"""Shared test infrastructure for the Claude Code and Codex Cognee integrations.

Building blocks (see SHARED_TEST_INFRA_SPEC.md):
  - suites        : Suite descriptors that parametrize tests over claude-code / codex
  - identity_fake : stateful login / api-key / agent / dataset fake
  - mock_cognee   : lightweight mock Cognee HTTP server built on pytest-httpserver
  - payloads      : synthetic stdin hook-payload builders (all hook types)
  - isolation     : temp-HOME state isolation for subprocess (e2e) and in-process (unit) tests
  - fixtures      : pytest fixtures wiring the above together

Nothing here writes to the real ~/.cognee-plugin — all state is redirected into a
per-test temporary HOME.
"""

from .suites import ALL_SUITES, CLAUDE, CODEX, Suite, config_dir, state_dir

__all__ = ["ALL_SUITES", "CLAUDE", "CODEX", "Suite", "config_dir", "state_dir"]
