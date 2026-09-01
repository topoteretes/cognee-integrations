"""Harness self-tests: temp-HOME isolation for subprocess and in-process tests."""

from __future__ import annotations

from utils import plugin_root, state_dir
from utils.isolation import DETERMINISTIC_ENV, build_env


def test_build_env_isolates_and_targets_mock(suite, temp_home, mock_server):
    env = build_env(suite, temp_home, service_url=mock_server.url, api_key="k")
    assert env["HOME"] == str(temp_home)
    assert env["USERPROFILE"] == str(temp_home)
    assert env["COGNEE_BASE_URL"] == mock_server.url
    assert env["COGNEE_PLATFORM_API_URL"] == mock_server.url
    assert env["COGNEE_API_KEY"] == "k"
    assert env[suite.cwd_env] == str(temp_home)
    for key, value in DETERMINISTIC_ENV.items():
        assert env[key] == value


def test_build_env_scrubs_inherited_vars_and_supports_unset_url(suite, temp_home, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", "https://leaked.example")
    monkeypatch.setenv("LLM_API_KEY", "leaked")
    env = build_env(suite, temp_home, service_url=None, api_key=None)
    assert "COGNEE_BASE_URL" not in env  # genuinely unset -> local-mode routing
    assert "LLM_API_KEY" not in env
    assert "COGNEE_API_KEY" not in env


def test_isolated_modules_bind_to_temp_home(
    suite, temp_home, isolated_modules, assert_clean_real_home
):
    config = isolated_modules(suite, "config")
    assert str(config._STATE_DIR).startswith(str(temp_home))
    assert config._STATE_DIR == state_dir(suite, temp_home)

    common = isolated_modules(suite, "_plugin_common")
    assert common._PLUGIN_DIR == state_dir(suite, temp_home)
    assert str(common._SERVER_READY_MARKER).startswith(str(plugin_root(temp_home)))


def test_isolated_config_reads_mock_url_and_defaults(
    suite, temp_home, mock_server, monkeypatch, isolated_modules
):
    config = isolated_modules(suite, "config")
    # Loading scrubs inherited COGNEE_* vars, so test env goes in afterwards;
    # load_config() reads the environment at call time.
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    loaded = config.load_config()
    assert loaded["base_url"] == mock_server.url
    assert config.get_dataset(loaded) == suite.default_dataset
    assert loaded["agent_name"] == suite.agent_name


def test_hook_module_loads_hyphenated_scripts(suite, hook_module, temp_home):
    watcher = hook_module(suite, "exit-watcher.py")
    assert watcher.__name__.endswith("_exit_watcher")
    assert callable(watcher._refresh_credits_marker)
