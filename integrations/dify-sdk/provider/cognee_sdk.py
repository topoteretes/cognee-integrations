import logging
from typing import Any

import httpx
from dify_plugin import ToolProvider
from dify_plugin.config.logger_format import plugin_logger_handler
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(plugin_logger_handler)


class CogneeSdkProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        base_url = credentials.get("base_url", "").rstrip("/")
        user_email = credentials.get("user_email", "")
        user_password = credentials.get("user_password", "")

        logger.info(f"Validating credentials for base_url={base_url}")

        if not base_url:
            raise ToolProviderCredentialValidationError("Cognee Server URL is required")
        if not user_email:
            raise ToolProviderCredentialValidationError("User Email is required")
        if not user_password:
            raise ToolProviderCredentialValidationError("User Password is required")

        # 1. Health check
        try:
            response = httpx.get(f"{base_url}/health", timeout=60)
            logger.info(f"Health check response: {response.status_code}")
            response.raise_for_status()
        except httpx.ConnectError:
            raise ToolProviderCredentialValidationError(
                f"Cannot connect to Cognee server at {base_url}. "
                "Is the server running? Start it with: docker compose up -d"
            )
        except httpx.HTTPStatusError as e:
            raise ToolProviderCredentialValidationError(
                f"Cognee health check failed with status "
                f"{e.response.status_code}: {e.response.text}"
            )
        except Exception as e:
            raise ToolProviderCredentialValidationError(
                f"Failed to connect to Cognee server: {str(e)}"
            )

        # 2. Login to verify credentials
        try:
            response = httpx.post(
                f"{base_url}/api/v1/auth/login",
                data={
                    "username": user_email,
                    "password": user_password,
                },
                timeout=60,
            )
            logger.info(f"Login response: {response.status_code}")
            response.raise_for_status()

            token_data = response.json()
            if "access_token" not in token_data:
                raise ToolProviderCredentialValidationError(
                    "Login succeeded but no access token was returned"
                )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                raise ToolProviderCredentialValidationError(
                    "Invalid email or password. Check your Cognee server credentials."
                )
            raise ToolProviderCredentialValidationError(
                f"Cognee login failed with status {e.response.status_code}: {e.response.text}"
            )
        except ToolProviderCredentialValidationError:
            raise
        except Exception as e:
            logger.error(f"Login validation failed: {e}")
            raise ToolProviderCredentialValidationError(
                f"Failed to authenticate with Cognee server: {str(e)}"
            )
