"""Shared helpers for the tools that call the Cognee API.

Kept deliberately small: HTTP client construction, credential normalization,
authentication, header construction, parameter parsing, JSON reading and
error formatting. Each tool still owns its request.

Authentication works against both deployments of the same ``/api/v1`` API:

* an **API key** (Cognee Cloud, or a self-hosted server with a key) is sent
  as ``X-Api-Key`` on every request;
* **email and password** (self-hosted default user) are exchanged for a
  bearer token at ``POST /api/v1/auth/login`` once per tool call.

The API key wins when both are configured.
"""

from typing import Any

import httpx

LOGIN_TIMEOUT = 60
API_PREFIX = "/api/v1"


# --- transport -------------------------------------------------------------------------


def make_client() -> httpx.Client:
    """A fresh HTTP client with the plugin's transport policy.

    ``trust_env=False`` keeps a system proxy from intercepting calls to a
    local server. ``follow_redirects=True`` is required by Cognee Cloud, whose
    gateway answers ``/api/v1/datasets`` with a 307 to the trailing-slash URL.
    Timeouts are set per request by each tool.
    """
    return httpx.Client(trust_env=False, follow_redirects=True)


# --- credentials -----------------------------------------------------------------------


def normalize_base_url(raw: str) -> str:
    """Server root the ``/api/v1`` routes hang off.

    Accepts ``https://host``, ``https://host/``, ``https://host/api`` and
    ``https://host/api/v1`` and returns ``https://host``. The ``/api`` form
    is what earlier versions of this plugin asked Cognee Cloud users to
    enter, so it keeps working after an upgrade.
    """
    url = (raw or "").strip().rstrip("/")
    for suffix in (API_PREFIX, "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


def normalize_credentials(credentials: dict[str, Any]) -> dict[str, str]:
    """The provider credentials with one agreed shape for every caller.

    Whitespace is stripped from the URL, key and email (the password is kept
    verbatim), so provider validation and tool execution see the same values.
    """
    return {
        "base_url": normalize_base_url(credentials.get("base_url", "")),
        "api_key": (credentials.get("api_key") or "").strip(),
        "user_email": (credentials.get("user_email") or "").strip(),
        "user_password": credentials.get("user_password") or "",
    }


def base_url_of(runtime: Any) -> str:
    return normalize_credentials(runtime.credentials)["base_url"]


# --- authentication --------------------------------------------------------------------


def login(client: httpx.Client, base_url: str, email: str, password: str) -> str:
    """Log in with the OAuth2 password form and return the bearer token."""
    response = client.post(
        f"{base_url}{API_PREFIX}/auth/login",
        data={"username": email, "password": password},
        timeout=LOGIN_TIMEOUT,
    )
    response.raise_for_status()
    return parse_json(response)["access_token"]


def auth_headers(
    credentials: dict[str, Any], client: httpx.Client, *, json: bool = False
) -> dict[str, str]:
    """Headers that authenticate a request for the given provider credentials.

    Raises ``ValueError`` when neither method is configured and
    ``httpx.HTTPStatusError`` when the login is refused.
    """
    creds = normalize_credentials(credentials)
    if creds["api_key"]:
        headers = {"X-Api-Key": creds["api_key"], "Accept": "application/json"}
    else:
        if not creds["user_email"] or not creds["user_password"]:
            raise ValueError("Configure either an API key or a user email and password for Cognee")
        token = login(client, creds["base_url"], creds["user_email"], creds["user_password"])
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if json:
        headers["Content-Type"] = "application/json"
    return headers


def authorize(client: httpx.Client, runtime: Any, *, json: bool = False) -> dict[str, str]:
    """``auth_headers`` for a tool's runtime credentials."""
    return auth_headers(runtime.credentials, client, json=json)


# --- parameters and responses ----------------------------------------------------------


def split_csv(value: Any) -> list[str]:
    """Split a comma-separated tool parameter into a list of non-empty items."""
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def as_bool(value: Any) -> bool:
    """Interpret a Dify select/boolean parameter (``"true"``/``True``) as a bool."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def parse_json(response: httpx.Response) -> Any:
    """The response body as JSON; a non-JSON body is an error, never a success.

    Every endpoint the tools parse promises a JSON body. Routes that answer with
    no content (the dataset and data deletes) are not parsed at all.
    """
    try:
        return response.json()
    except ValueError as exc:
        snippet = response.text[:200].strip()
        raise ValueError(
            f"Cognee returned a non-JSON response (HTTP {response.status_code})"
            + (f": {snippet}" if snippet else "")
        ) from exc


def error_text(exc: Exception, action: str) -> str:
    """One-line description of a failed call, with the server body when there is one."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Cognee API error {exc.response.status_code}: {exc.response.text}"
    return f"{action}: {exc}"
