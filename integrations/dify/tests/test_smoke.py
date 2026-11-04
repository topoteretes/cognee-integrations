"""Packaging: versions, compose file, provider registration and credential validation."""

import re

import pytest
import yaml
from conftest import login_ok

COGNEE_VERSION = "1.6.1"


def test_imports():
    from dify_plugin import DifyPluginEnv, Plugin

    assert Plugin is not None
    assert DifyPluginEnv is not None


# --- docker-compose.yml: the server setup cognee 1.6 needs -------------------


def _compose_text(root) -> str:
    return (root / "docker" / "docker-compose.yml").read_text(encoding="utf-8")


def test_compose_pins_the_tested_cognee_image(plugin_root):
    assert f"image: cognee/cognee:{COGNEE_VERSION}" in _compose_text(plugin_root)


def test_compose_gives_the_default_user_a_password(plugin_root):
    # Since cognee 1.6 the default user can only log in when the server was
    # started with DEFAULT_USER_PASSWORD; the plugin logs in with a password.
    text = _compose_text(plugin_root)
    assert "DEFAULT_USER_PASSWORD=default_password" in text
    assert "DEFAULT_USER_EMAIL=default_user@example.com" in text


def test_compose_persists_the_1_6_storage_paths(plugin_root):
    text = _compose_text(plugin_root)
    assert ":/cognee-storage/system" in text
    assert ":/cognee-storage/data" in text
    assert "/app/cognee/.cognee_system" not in text


def test_readme_and_manifest_agree_on_versions(plugin_root):
    readme = (plugin_root / "README.md").read_text(encoding="utf-8")
    manifest = (plugin_root / "manifest.yaml").read_text(encoding="utf-8")
    pyproject = (plugin_root / "pyproject.toml").read_text(encoding="utf-8")

    assert f"Tested with Cognee v{COGNEE_VERSION}" in readme
    assert f"image: cognee/cognee:{COGNEE_VERSION}" in readme
    assert f"pip install cognee=={COGNEE_VERSION}" in readme

    plugin_version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert f"version: {plugin_version}" in manifest
    assert f"**Version:** {plugin_version}" in readme


# --- registration ---------------------------------------------------------------


def test_provider_registers_every_tool_yaml(plugin_root):
    provider = yaml.safe_load((plugin_root / "provider" / "cognee.yaml").read_text())
    registered = set(provider["tools"])
    on_disk = {f"tools/{p.name}" for p in (plugin_root / "tools").glob("*.yaml")}
    assert registered == on_disk
    assert set(provider["credentials_for_provider"]) == {
        "base_url",
        "api_key",
        "user_email",
        "user_password",
    }


def test_tool_yaml_points_at_its_source_and_is_fully_localized(plugin_root):
    for path in (plugin_root / "tools").glob("*.yaml"):
        spec = yaml.safe_load(path.read_text())
        name = spec["identity"]["name"]
        assert path.stem == name
        assert spec["extra"]["python"]["source"] == f"tools/{name}.py"
        assert (plugin_root / "tools" / f"{name}.py").is_file()
        for param in spec["parameters"]:
            assert set(param["label"]) == {"en_US", "zh_Hans", "pt_BR", "ja_JP"}, (name, param)


def test_tool_descriptions_are_deployment_neutral(plugin_root):
    # The plugin serves Cognee Cloud and self-hosted servers alike.
    for path in (plugin_root / "tools").glob("*.yaml"):
        text = path.read_text()
        assert not re.search(
            r"local Cognee|本地 Cognee|servidor Cognee local|ローカル Cognee", text
        ), path


def test_forget_everything_is_form_only(plugin_root):
    spec = yaml.safe_load((plugin_root / "tools" / "forget.yaml").read_text())
    everything = next(p for p in spec["parameters"] if p["name"] == "everything")
    assert everything["form"] == "form"
    assert everything["default"] == "false"


# --- provider: login failure messages ----------------------------------------


def test_login_error_maps_the_password_less_default_user():
    from provider.cognee import login_error_message

    body = '{"detail":"This user does not have a password. Use API key authentication."}'
    message = login_error_message(400, body)
    assert "DEFAULT_USER_PASSWORD" in message
    assert "API key" in message
    assert "Invalid email or password" not in message


def test_login_error_keeps_the_wrong_password_message():
    from provider.cognee import login_error_message

    message = login_error_message(400, '{"detail":"LOGIN_BAD_CREDENTIALS"}')
    assert message.startswith("Invalid email or password")
    assert "DEFAULT_USER_PASSWORD" in message


def test_login_error_reports_other_statuses_verbatim():
    from provider.cognee import login_error_message

    assert login_error_message(503, "down") == "Cognee login failed with status 503: down"


def _validate(credentials):
    from provider.cognee import CogneeProvider

    provider = CogneeProvider.__new__(CogneeProvider)
    provider._validate_credentials(credentials)


def test_validate_credentials_surfaces_the_default_user_hint(fake_cognee):
    from dify_plugin.errors.tool import ToolProviderCredentialValidationError

    fake_cognee.on("GET", "/health", json_body={"status": "ready"})
    fake_cognee.on(
        "POST",
        "/api/v1/auth/login",
        status=400,
        json_body={"detail": "This user does not have a password. Use API key authentication."},
    )
    with pytest.raises(ToolProviderCredentialValidationError) as excinfo:
        _validate(
            {
                "base_url": "http://cognee.test",
                "user_email": "default_user@example.com",
                "user_password": "default_password",
            }
        )
    assert "DEFAULT_USER_PASSWORD" in str(excinfo.value)


def test_validate_credentials_logs_in_without_an_api_key(fake_cognee):
    fake_cognee.on("GET", "/health", json_body={"status": "ready"})
    fake_cognee.on("POST", "/api/v1/auth/login", login_ok)
    _validate(
        {
            "base_url": "http://cognee.test/",
            "api_key": "  ",
            "user_email": " u@e ",
            "user_password": "p",
        }
    )
    assert len(fake_cognee.calls("POST", "/api/v1/auth/login")) == 1
    assert fake_cognee.calls("GET", "/api/v1/datasets") == []
