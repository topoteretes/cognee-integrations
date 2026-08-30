"""The /openapi manifest is the contract with tapes — assert it stays honest."""

from cognee_integration_tapes_cassette.manifest import build_openapi_spec


def spec(config):
    return build_openapi_spec(config)


def test_manifest_kind_and_identity(config):
    cassette = spec(config)["x-tapes-cassette"]
    assert cassette["kind"] == "cassette/v1alpha1"
    assert cassette["cassette"]["name"] == "cognee"
    assert cassette["cassette"]["port"] == config.port
    assert cassette["depends"] == {"core": "v1", "views": []}


def test_manifest_api_block_matches_served_routes(config, app):
    cassette = spec(config)["x-tapes-cassette"]
    served = {
        route.path: methods
        for route, methods in ((r, getattr(r, "methods", set())) for r in app.routes)
    }
    assert cassette["api"]["health"] == "/ping"
    assert cassette["api"]["openapi"] == "/openapi"
    assert "GET" in served["/ping"]
    assert "GET" in served["/openapi"]


def test_every_manifest_path_is_served(config, app):
    served = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }
    for path, operations in spec(config)["paths"].items():
        for method in operations:
            assert (path, method.upper()) in served, f"{method.upper()} {path} not served"


def test_manifest_paths_use_prefix_path(config):
    document = spec(config)
    prefix = document["x-tapes-cassette"]["api"]["prefix_path"]
    for path in document["paths"]:
        assert path.startswith(f"/{prefix}/")


def test_mcp_tools_are_post_only_with_annotations(config):
    tool_names = set()
    for operations in spec(config)["paths"].values():
        for method, operation in operations.items():
            mcp = operation.get("x-tapes-mcp")
            if mcp is None:
                continue
            # v1alpha1 converts only POST routes into MCP tools.
            assert method == "post"
            assert set(mcp["annotations"]) == {
                "readOnlyHint",
                "idempotentHint",
                "openWorldHint",
            }
            tool_names.add(mcp["name"])
    assert tool_names == {"sync_sessions", "sync_status", "search_memory"}
