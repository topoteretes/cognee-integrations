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


class AddDataTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = self.runtime.credentials["base_url"].rstrip("/")
        user_email = self.runtime.credentials["user_email"]
        user_password = self.runtime.credentials["user_password"]

        dataset_name = tool_parameters.get("dataset_name", "")
        dataset_id = tool_parameters.get("dataset_id", "")
        text_data = tool_parameters["text_data"]
        node_set = tool_parameters.get("node_set", "")

        if not dataset_name and not dataset_id:
            error_msg = "Either dataset_name or dataset_id must be provided"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
            return

        try:
            with httpx.Client(trust_env=False) as client:
                token = _login(base_url, user_email, user_password, client)

                text_bytes = text_data.encode("utf-8")
                file_obj = io.BytesIO(text_bytes)

                form_data: dict[str, Any] = {}
                if dataset_name:
                    form_data["datasetName"] = dataset_name
                if dataset_id:
                    form_data["datasetId"] = dataset_id
                if node_set:
                    node_set_list = [n.strip() for n in node_set.split(",") if n.strip()]
                    for ns in node_set_list:
                        form_data.setdefault("node_set", []).append(ns)

                filename = f"data_{uuid.uuid4().hex[:8]}.txt"
                response = client.post(
                    f"{base_url}/api/v1/add",
                    headers={"Authorization": f"Bearer {token}"},
                    files={"data": (filename, file_obj, "text/plain")},
                    data=form_data,
                    timeout=3600,
                )
                response.raise_for_status()
                result = response.json()

                resp_dataset_id = result.get("dataset_id", dataset_id or "")
                resp_dataset_name = result.get("dataset_name", dataset_name or "")
                data_ids: list[str] = []
                for info in result.get("data_ingestion_info", []):
                    if isinstance(info, dict) and info.get("data_id"):
                        data_ids.append(str(info["data_id"]))

                first_data_id = data_ids[0] if data_ids else ""

                yield self.create_json_message(result)
                yield self.create_variable_message("dataset_name", resp_dataset_name)
                yield self.create_variable_message("dataset_id", resp_dataset_id)
                yield self.create_variable_message("data_id", first_data_id)
                yield self.create_variable_message("items_count", len(data_ids))
                yield self.create_text_message(
                    f"Successfully added data to dataset '{resp_dataset_name}' (id: {resp_dataset_id})."
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Cognee API error {e.response.status_code}: {e.response.text}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
        except Exception as e:
            error_msg = f"Failed to add data: {str(e)}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
