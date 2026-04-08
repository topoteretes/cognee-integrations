import io
import uuid
from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage


def _login(base_url: str, email: str, password: str, client: httpx.Client) -> str:
    response = client.post(
        f"{base_url}/api/v1/auth/login",
        data={"username": email, "password": password},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["access_token"]


class UpdateDataTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = self.runtime.credentials["base_url"].rstrip("/")
        user_email = self.runtime.credentials["user_email"]
        user_password = self.runtime.credentials["user_password"]

        dataset_id = tool_parameters["dataset_id"]
        data_id = tool_parameters["data_id"]
        text_data = tool_parameters["text_data"]
        node_set = tool_parameters.get("node_set", "")

        try:
            with httpx.Client(trust_env=False) as client:
                token = _login(base_url, user_email, user_password, client)

                text_bytes = text_data.encode("utf-8")
                file_obj = io.BytesIO(text_bytes)
                filename = f"data_{uuid.uuid4().hex[:8]}.txt"

                form_data: dict[str, Any] = {}
                if node_set:
                    node_set_list = [n.strip() for n in node_set.split(",") if n.strip()]
                    for ns in node_set_list:
                        form_data.setdefault("node_set", []).append(ns)

                response = client.patch(
                    f"{base_url}/api/v1/update",
                    params={"data_id": data_id, "dataset_id": dataset_id},
                    headers={"Authorization": f"Bearer {token}"},
                    files={"data": (filename, file_obj, "text/plain")},
                    data=form_data,
                    timeout=21600,
                )
                response.raise_for_status()

                try:
                    result = response.json()
                except Exception:
                    result = {"status": "ok"}

                yield self.create_json_message(result)
                yield self.create_variable_message("succeeded", True)
                yield self.create_variable_message("dataset_id", dataset_id)
                yield self.create_variable_message("data_id", data_id)
                yield self.create_text_message(
                    f"Successfully updated data item '{data_id}' in dataset '{dataset_id}'."
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Cognee API error {e.response.status_code}: {e.response.text}"
            yield self.create_json_message({"succeeded": False, "error": error_msg})
            yield self.create_variable_message("succeeded", False)
            yield self.create_variable_message("dataset_id", dataset_id)
            yield self.create_variable_message("data_id", data_id)
            yield self.create_text_message(error_msg)
        except Exception as e:
            error_msg = f"Failed to update data: {str(e)}"
            yield self.create_json_message({"succeeded": False, "error": error_msg})
            yield self.create_variable_message("succeeded", False)
            yield self.create_variable_message("dataset_id", dataset_id)
            yield self.create_variable_message("data_id", data_id)
            yield self.create_text_message(error_msg)
