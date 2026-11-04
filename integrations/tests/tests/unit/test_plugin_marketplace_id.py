"""The plugin id is ``<plugin>@<marketplace>``, and the marketplace half varies.

An install from this repo's own marketplace is registered as
``cognee-memory@cognee``; one from the official directory as
``cognee-memory@anthropic-plugin-directory``. Two readers used to hard-code the
first form:

  1. ``cognee_statusline_render._plugin_enabled`` — the self-eviction check. With
     a directory install it never found its key, concluded the plugin was
     disabled, and deleted its own statusLine entry on the first refresh after
     every SessionStart re-added it (the bar flashed once, then vanished).
  2. ``session-start._apply_update_nudge`` — told directory users to run
     ``/plugin update cognee-memory@cognee``, which Claude Code rejects.

claude-code only: the other suites have no plugin registry or enabledPlugins.
"""

from __future__ import annotations

import pytest
from utils.statusline import write_json


@pytest.fixture
def renderer(suite, statusline):
    if not hasattr(statusline, "_PLUGIN_KEY_PREFIX"):
        pytest.skip(f"{suite.name}: renderer has no enabledPlugins self-eviction check")
    return statusline


@pytest.fixture
def common(suite, isolated_modules):
    mod = isolated_modules(suite, "_plugin_common")
    if not hasattr(mod, "installed_plugin_id"):
        pytest.skip(f"{suite.name}: no plugin install registry")
    return mod


def _settings(renderer, enabled: dict | None) -> None:
    payload = {} if enabled is None else {"enabledPlugins": enabled}
    write_json(renderer._USER_SETTINGS, payload)


# ── self-eviction check ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    ["cognee-memory@cognee", "cognee-memory@anthropic-plugin-directory", "cognee-memory@my-fork"],
)
def test_enabled_under_any_marketplace(renderer, key):
    _settings(renderer, {key: True})
    assert renderer._plugin_enabled("") is True


def test_disabled_when_every_marketplace_entry_is_false(renderer):
    _settings(
        renderer, {"cognee-memory@cognee": False, "cognee-memory@anthropic-plugin-directory": False}
    )
    assert renderer._plugin_enabled("") is False


def test_enabled_when_one_marketplace_entry_is_true(renderer):
    """Stale false from an uninstalled marketplace must not evict a live install."""
    _settings(
        renderer, {"cognee-memory@cognee": False, "cognee-memory@anthropic-plugin-directory": True}
    )
    assert renderer._plugin_enabled("") is True


def test_other_plugins_do_not_count(renderer):
    _settings(renderer, {"cognee-memory-fork@cognee": True, "other@cognee": True})
    assert renderer._plugin_enabled("") is False


def test_absent_key_is_not_enabled(renderer):
    _settings(renderer, None)
    assert renderer._enabled_in(renderer._USER_SETTINGS) is None
    assert renderer._plugin_enabled("") is False


# ── update nudge id ────────────────────────────────────────────────────────


def _registry(entries: dict) -> dict:
    return {
        "version": 2,
        "plugins": {
            key: [{"scope": "user", "version": v} for v in versions]
            for key, versions in entries.items()
        },
    }


def test_installed_id_from_directory_registry(common):
    write_json(
        common._INSTALLED_PLUGINS_FILE,
        _registry({"cognee-memory@anthropic-plugin-directory": ["1.6.5"]}),
    )
    assert common.installed_plugin_id() == "cognee-memory@anthropic-plugin-directory"


def test_installed_id_prefers_newest_when_both_marketplaces_present(common):
    write_json(
        common._INSTALLED_PLUGINS_FILE,
        _registry(
            {
                "cognee-memory@cognee": ["1.6.4"],
                "cognee-memory@anthropic-plugin-directory": ["1.6.5"],
            }
        ),
    )
    assert common.installed_plugin_id() == "cognee-memory@anthropic-plugin-directory"


def test_installed_id_falls_back_without_registry(common):
    assert common.installed_plugin_id() == "cognee-memory@cognee"


def test_installed_id_ignores_other_plugins_and_malformed(common):
    for payload in ({}, {"plugins": []}, {"plugins": {"other@cognee": [{"version": "1.0.0"}]}}):
        write_json(common._INSTALLED_PLUGINS_FILE, payload)
        assert common.installed_plugin_id() == "cognee-memory@cognee", payload
