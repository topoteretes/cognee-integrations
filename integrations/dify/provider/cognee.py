import logging
from typing import Any

import httpx
from dify_plugin import ToolProvider
from dify_plugin.config.logger_format import plugin_logger_handler
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(plugin_logger_handler)


class CogneeProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        base_url = credentials.get("base_url", "").rstrip("/")
        api_key = credentials.get("api_key", "")

        logger.info(f"Validating credentials for base_url={base_url}")

        if not base_url:
            raise ToolProviderCredentialValidationError("Base URL is required")
        if not api_key:
            raise ToolProviderCredentialValidationError("API Key is required")

        try:
            response = httpx.get(
                f"{base_url}/health",
                headers={"X-Api-Key": api_key},
                timeout=10,
            )
            logger.info(f"Health check response: {response.status_code}")
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise ToolProviderCredentialValidationError(
                f"Cognee API returned {e.response.status_code}: {e.response.text}"
            )
        except Exception as e:
            logger.error(f"Credential validation failed: {e}")
            raise ToolProviderCredentialValidationError(
                f"Failed to connect to Cognee API: {str(e)}"
            )
