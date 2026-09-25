"""E2e canary: a real hook script runs as a subprocess against the mock server.

Proves the whole harness chain — payload builders, subprocess isolation, env
injection, mock routing — before deeper e2e suites build on it.
"""

from __future__ import annotations


def test_run_hook_session_context_lookup(
    suite, run_hook, mock_server, payloads, assert_clean_real_home
):
    result = run_hook(
        suite,
        "session-context-lookup.py",
        stdin=payloads.user_prompt(prompt="what did we decide?"),
        service_url=mock_server.url,
    )
    assert result.returncode == 0, result.stderr
