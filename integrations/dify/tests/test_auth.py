"""Credential normalization, authentication headers and the API-key provider check."""

import httpx
import pytest
from conftest import login_ok

# --- base url and credential normalization ----------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://tenant-xxx.cloud.cognee.ai",
        "https://tenant-xxx.cloud.cognee.ai/",
        "https://tenant-xxx.cloud.cognee.ai/api",
        "https://tenant-xxx.cloud.cognee.ai/api/",
        "https://tenant-xxx.cloud.cognee.ai/api/v1",
        "  https://tenant-xxx.cloud.cognee.ai/api/v1/  ",
    ],
)
def test_normalize_base_url_strips_api_suffixes(raw):
    from tools.cognee_client import normalize_base_url

    assert normalize_base_url(raw) == "https://tenant-xxx.cloud.cognee.ai"


def test_normalize_base_url_keeps_other_paths():
    from tools.cognee_client import normalize_base_url

    assert normalize_base_url("http://localhost:8000") == "http://localhost:8000"
    assert normalize_base_url("http://proxy/cognee/") == "http://proxy/cognee"
    assert normalize_base_url("") == ""


def test_normalize_credentials_strips_everything_but_the_password():
    from tools.cognee_client import normalize_credentials

    creds = normalize_credentials(
        {
            "base_url": " http://x/api ",
            "api_key": " k ",
            "user_email": " u@e ",
            "user_password": " p ",
        }
    )
    assert creds == {
        "base_url": "http://x",
        "api_key": "k",
        "user_email": "u@e",
        "user_password": " p ",
    }
    assert normalize_credentials({})["api_key"] == ""


# --- auth headers -----------------------------------------------------------------


def test_api_key_wins_and_skips_login(fake_cognee):
    from tools.cognee_client import auth_headers

    headers = auth_headers(
        {"base_url": "http://x", "api_key": " key-1 ", "user_email": "a@b", "user_password": "p"},
        fake_cognee.client(),
        json=True,
    )
    assert headers == {
        "X-Api-Key": "key-1",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    assert fake_cognee.requests == []


def test_email_password_logs_in_against_the_normalized_url(fake_cognee):
    from tools.cognee_client import auth_headers

    fake_cognee.on("POST", "/api/v1/auth/login", login_ok)
    headers = auth_headers(
        {
            "base_url": "http://cognee.test/api",
            "api_key": "",
            "user_email": " u@e ",
            "user_password": "pw",
        },
        fake_cognee.client(),
    )
    assert headers == {"Authorization": "Bearer tok", "Accept": "application/json"}
    request = fake_cognee.calls("POST", "/api/v1/auth/login")[0]
    assert str(request.url) == "http://cognee.test/api/v1/auth/login"
    assert b"username=u%40e&password=pw" == request.content


def test_missing_credentials_raise_a_clear_error(fake_cognee):
    from tools.cognee_client import auth_headers

    with pytest.raises(ValueError, match="API key or a user email and password"):
        auth_headers({"base_url": "http://x", "user_email": "u@e"}, fake_cognee.client())


# --- provider validation with an API key -------------------------------------------


def _validate(credentials):
    from provider.cognee import CogneeProvider

    provider = CogneeProvider.__new__(CogneeProvider)
    provider._validate_credentials(credentials)


def test_provider_accepts_an_api_key_via_the_datasets_route(fake_cognee):
    fake_cognee.on("GET", "/health", json_body={"status": "ready"})
    fake_cognee.on("GET", "/api/v1/datasets", json_body=[])
    _validate({"base_url": "https://t.cloud.cognee.ai/api", "api_key": " k "})
    request = fake_cognee.calls("GET", "/api/v1/datasets")[0]
    assert str(request.url) == "https://t.cloud.cognee.ai/api/v1/datasets"
    assert request.headers["X-Api-Key"] == "k"
    assert fake_cognee.calls("POST", "/api/v1/auth/login") == []


def test_provider_rejects_a_bad_api_key(fake_cognee):
    from dify_plugin.errors.tool import ToolProviderCredentialValidationError

    fake_cognee.on("GET", "/health", json_body={"status": "ready"})
    fake_cognee.on("GET", "/api/v1/datasets", status=401, json_body={"detail": "Unauthorized"})
    with pytest.raises(ToolProviderCredentialValidationError, match="API key was rejected"):
        _validate({"base_url": "http://cognee.test", "api_key": "bad"})


def test_provider_requires_one_credential_method(fake_cognee):
    from dify_plugin.errors.tool import ToolProviderCredentialValidationError

    with pytest.raises(ToolProviderCredentialValidationError, match="either an API Key"):
        _validate({"base_url": "http://cognee.test", "user_email": "u@e"})
    assert fake_cognee.requests == []  # rejected before any request


def test_provider_reports_an_unreachable_server(monkeypatch):
    import tools.cognee_client as cognee_client
    from dify_plugin.errors.tool import ToolProviderCredentialValidationError

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    real_client = httpx.Client
    monkeypatch.setattr(
        cognee_client.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(refuse), **kw),
    )
    with pytest.raises(ToolProviderCredentialValidationError, match="Cannot connect"):
        _validate({"base_url": "http://cognee.test", "api_key": "k"})
