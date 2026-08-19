"""Regression tests for the exit-watcher's session-long credits refresh.

The exit-watcher is the only plugin process whose lifetime matches the host
session — the idle watcher exits at ``bridge_complete`` minutes after the
last activity, which is exactly why a credits poll there let the status-line
segment age out of its 15-minute TTL during longer idle stretches ("credits
disappeared after the terminal was open for a while"). These pin the gate:
cloud URL (bootstrap value or env) → refresh; local/none → skip; fresh
tenant entry → throttled.

Migrated from {claude-code,codex}/tests/test_exit_watcher_credits.py.
"""

from __future__ import annotations

import time

import pytest

_CLOUD_URL = "https://tenant-f8c21da4-6674-4cc5-bc56-de5e93db881d.aws.cognee.ai"


@pytest.fixture
def drive(suite, hook_module, isolated_modules, monkeypatch):
    """Run _refresh_credits_marker with patched seams; return the call counter.

    The watcher imports _plugin_common function-locally, so the load order
    matters: the watcher module first, then a fresh _plugin_common — the
    watcher's late import binds to that same sys.modules entry, so the patches
    below are what it sees.
    """

    def _drive(*, service_url, env_url=None, marker=None):
        watcher = hook_module(suite, "exit-watcher.py")
        pc = isolated_modules(suite, "_plugin_common")
        monkeypatch.setattr(watcher, "_log", lambda *a, **k: None)
        if env_url is not None:
            monkeypatch.setenv("COGNEE_BASE_URL", env_url)

        calls = {"refreshes": 0}
        monkeypatch.setattr(
            pc,
            "refresh_credits",
            lambda *a, **k: calls.update(refreshes=calls["refreshes"] + 1) or {},
        )
        monkeypatch.setattr(pc, "read_credits_marker", lambda: marker or {})
        watcher._refresh_credits_marker(service_url)
        return calls

    return _drive


def test_cloud_bootstrap_url_refreshes(drive):
    assert drive(service_url=_CLOUD_URL)["refreshes"] == 1


def test_empty_bootstrap_falls_back_to_env(drive):
    assert drive(service_url="", env_url=_CLOUD_URL)["refreshes"] == 1


def test_local_bootstrap_skips(drive):
    assert drive(service_url="http://localhost:8011")["refreshes"] == 0


def test_no_url_anywhere_skips(drive):
    # COGNEE_BASE_URL / COGNEE_LOCAL_API_URL are scrubbed by the harness.
    assert drive(service_url="")["refreshes"] == 0


def test_fresh_tenant_entry_throttles(drive):
    fresh = {
        "f8c21da4-6674-4cc5-bc56-de5e93db881d": {
            "base_url": _CLOUD_URL,
            "checked_at": time.time(),
        }
    }
    assert drive(service_url=_CLOUD_URL, marker=fresh)["refreshes"] == 0
