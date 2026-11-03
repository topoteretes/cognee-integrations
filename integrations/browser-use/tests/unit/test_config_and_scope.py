import pytest
from cognee_integration_browser_use import ConfigError, MemoryScope, resolve_config

BASE = {"COGNEE_BASE_URL": "http://cognee.test:8000/", "COGNEE_API_KEY": "ck_x"}


def test_precedence_explicit_env_default():
    config = resolve_config({**BASE, "COGNEE_BROWSER_USE_RECALL_TOP_K": "7"}, recall_max_chars=1234)
    assert config.base_url == "http://cognee.test:8000"
    assert config.recall_top_k == 7
    assert config.recall_max_chars == 1234
    assert config.recall_timeout_s == 8.0
    assert config.remember_tool_enabled is False
    assert config.capture_enabled is True


def test_required_and_invalid_values():
    with pytest.raises(ConfigError, match="COGNEE_API_KEY"):
        resolve_config({"COGNEE_BASE_URL": "http://x"})
    with pytest.raises(ConfigError, match="must be a boolean"):
        resolve_config({**BASE, "COGNEE_BROWSER_USE_CAPTURE": "maybe"})
    with pytest.raises(ConfigError, match="non-negative integer"):
        resolve_config({**BASE, "COGNEE_BROWSER_USE_RECALL_TOP_K": "many"})
    with pytest.raises(ConfigError, match="not a valid dataset name"):
        resolve_config(BASE, dataset="../etc")
    with pytest.raises(ConfigError, match="between 0 and 20"):
        resolve_config({}, api_key="k", max_remember_calls_per_run=21)
    with pytest.raises(ConfigError, match="between 0 and 20"):
        resolve_config({}, api_key="k", max_remember_calls_per_run=-1)
    assert (
        resolve_config({}, api_key="k", max_remember_calls_per_run=20).max_remember_calls_per_run
        == 20
    )
    with pytest.raises(ConfigError, match="unknown configuration keys"):
        resolve_config(BASE, nope=1)


def test_scope_key_is_opaque_and_stable():
    a = MemoryScope(tenant_id="acme", user_id="u1")
    b = MemoryScope(tenant_id="acme", user_id="u1")
    c = MemoryScope(tenant_id="acme", user_id="u2")
    assert a.key == b.key
    assert a.key != c.key
    assert "acme" not in a.dataset_name()
    assert a.dataset_name().startswith("browser_use_")
    assert MemoryScope().is_empty
    assert MemoryScope(project_id="p").key != MemoryScope(user_id="p").key


def test_redacted_config_hides_key():
    config = resolve_config(BASE)
    assert config.redacted()["api_key"] == "<set>"
    assert "ck_x" not in str(config.redacted())
