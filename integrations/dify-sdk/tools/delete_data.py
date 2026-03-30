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


class DeleteDataTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = self.runtime.credentials["base_url"].rstrip("/")
        user_email = self.runtime.credentials["user_email"]
        user_password = self.runtime.credentials["user_password"]

        dataset_id = tool_parameters["dataset_id"]
        data_id = tool_parameters["data_id"]

        try:
            token = _login(base_url, user_email, user_password)

            response = httpx.delete(
                f"{base_url}/api/v1/datasets/{dataset_id}/data/{data_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            response.raise_for_status()

            yield self.create_json_message(
                {"succeeded": True, "dataset_id": dataset_id, "data_id": data_id}
            )
            yield self.create_variable_message("succeeded", True)
            yield self.create_variable_message("dataset_id", dataset_id)
            yield self.create_variable_message("data_id", data_id)
            yield self.create_text_message(
                f"Successfully deleted data item '{data_id}' from dataset '{dataset_id}'."
            )
        except Exception as e:
            yield self.create_json_message(
                {"succeeded": False, "dataset_id": dataset_id, "data_id": data_id}
            )
            yield self.create_variable_message("succeeded", False)
            yield self.create_variable_message("dataset_id", dataset_id)
            yield self.create_variable_message("data_id", data_id)
            yield self.create_text_message(f"Failed to delete data: {str(e)}")
