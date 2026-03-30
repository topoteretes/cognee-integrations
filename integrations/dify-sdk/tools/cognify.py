from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage


def _login(base_url: str, email: str, password: str) -> str:
    response = httpx.post(
        f"{base_url}/api/v1/auth/login",
        data={"username": email, "password": password},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["access_token"]


class CognifyTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = self.runtime.credentials["base_url"].rstrip("/")
        user_email = self.runtime.credentials["user_email"]
        user_password = self.runtime.credentials["user_password"]

        datasets_str = tool_parameters.get("datasets", "")
        dataset_ids_str = tool_parameters.get("dataset_ids", "")
        custom_prompt = tool_parameters.get("custom_prompt", "")
        ontology_key_str = tool_parameters.get("ontology_key", "")

        datasets = [d.strip() for d in datasets_str.split(",") if d.strip()] if datasets_str else []
        dataset_ids = (
            [d.strip() for d in dataset_ids_str.split(",") if d.strip()] if dataset_ids_str else []
        )

        if not datasets and not dataset_ids:
            error_msg = "Either datasets or dataset_ids must be provided"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
            return

        body: dict[str, Any] = {}
        if datasets:
            body["datasets"] = datasets
        if dataset_ids:
            body["datasetIds"] = dataset_ids
        if custom_prompt:
            body["customPrompt"] = custom_prompt
        if ontology_key_str:
            body["ontologyKey"] = [k.strip() for k in ontology_key_str.split(",") if k.strip()]

        try:
            token = _login(base_url, user_email, user_password)

            response = httpx.post(
                f"{base_url}/api/v1/cognify",
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=1200,
            )
            response.raise_for_status()

            try:
                result = response.json()
            except Exception:
                result = {"status": "ok"}

            label = ", ".join(datasets) if datasets else ", ".join(dataset_ids)
            yield self.create_json_message(result)
            yield self.create_variable_message("datasets", label)
            yield self.create_text_message(f"Successfully cognified dataset(s): {label}")
        except httpx.HTTPStatusError as e:
            error_msg = f"Cognee API error {e.response.status_code}: {e.response.text}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
        except Exception as e:
            error_msg = f"Failed to cognify: {str(e)}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
