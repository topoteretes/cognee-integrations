"""Settings parsing — onboarding-critical, so the required-token error is tested."""

import pytest
from cognee_integration_telegram.config import Settings


def test_from_env_reads_and_strips_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  123456:ABC-DEF  ")
    settings = Settings.from_env()
    assert settings.bot_token == "123456:ABC-DEF"


def test_from_env_missing_token_raises(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env()


def test_from_env_blank_token_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env()


def test_from_env_reads_cognee_server(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("COGNEE_BASE_URL", "http://cognee:9000")
    monkeypatch.setenv("COGNEE_API_KEY", "secret")
    settings = Settings.from_env()
    assert settings.cognee_base_url == "http://cognee:9000"
    assert settings.cognee_api_key == "secret"


def test_cognee_base_url_defaults_local(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.delenv("COGNEE_BASE_URL", raising=False)
    monkeypatch.delenv("COGNEE_API_KEY", raising=False)
    settings = Settings.from_env()
    assert settings.cognee_base_url == "http://localhost:8000"
    assert settings.cognee_api_key == ""
