from cognee_integration_aider.config import AiderCogneeConfig
from cognee_integration_aider.tools import cognee_tool_specs, remember_kwargs, search_kwargs


def test_remember_kwargs_are_project_scoped():
    config = AiderCogneeConfig(dataset="repo-memory", session_prefix="aider", project_id="repo")

    kwargs = remember_kwargs(config)

    assert kwargs == {
        "session_id": "aider:repo:default",
        "dataset_name": "repo-memory",
        "self_improvement": False,
    }


def test_search_kwargs_include_top_k():
    config = AiderCogneeConfig(dataset="repo-memory", project_id="repo", top_k=9)

    kwargs = search_kwargs(config, session_id="debug")

    assert kwargs["session_id"] == "aider:repo:debug"
    assert kwargs["dataset_name"] == "repo-memory"
    assert kwargs["top_k"] == 9


def test_tool_specs_are_json_schema_like():
    specs = cognee_tool_specs()

    assert [spec["name"] for spec in specs] == ["cognee_remember", "cognee_search"]
    assert specs[0]["parameters"]["required"] == ["data"]
    assert specs[1]["parameters"]["required"] == ["query_text"]
