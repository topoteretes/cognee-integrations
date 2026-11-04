"""The mode word in the "Cognee Memory Connected" message (session-start.py).

It must agree with the status line: a forced backend first, then whether the
target host is loopback. The old test, "is a URL set", ran after the bootstrap
had filled the localhost default into ``config["base_url"]`` and so said cloud
for every local session, a forced-local terminal included.

claude-code only: the other suites build their start message differently.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def ss(suite, hook_module):
    module = hook_module(suite, "session-start.py")
    if not hasattr(module, "_mode_word"):
        pytest.skip(f"{suite.name}: no _mode_word")
    return module


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"base_url": "http://localhost:8011"}, "local"),
        ({"base_url": "http://127.0.0.1:8011"}, "local"),
        ({"base_url": "http://[::1]:8011"}, "local"),
        ({"base_url": ""}, "local"),
        ({"base_url": "https://tenant.aws.cognee.ai"}, "cloud"),
        # The switch wins over whatever URL the bootstrap filled in.
        ({"base_url": "http://localhost:8011", "_forced_backend": "local"}, "local"),
        ({"base_url": "https://tenant.aws.cognee.ai", "_forced_backend": "local"}, "local"),
        # Forced cloud with no URL is still cloud (the status line names what is missing).
        ({"base_url": "", "_forced_backend": "cloud"}, "cloud"),
        ({"base_url": "https://tenant.aws.cognee.ai", "_forced_backend": "cloud"}, "cloud"),
    ],
)
def test_mode_word_matches_the_status_line(ss, config, expected):
    assert ss._mode_word(config) == expected
