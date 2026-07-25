import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_provider_imports():
    from cognee_integration_hermes import CogneeMemoryProvider

    provider = CogneeMemoryProvider()
    assert provider.name == "cognee"
    assert {schema["name"] for schema in provider.get_tool_schemas()} == {
        "cognee_recall",
        "cognee_remember",
        "cognee_forget",
    }


def test_directory_plugin_registers_provider():
    import importlib.util

    spec = importlib.util.spec_from_file_location("hermes_cognee_plugin", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class Collector:
        def __init__(self):
            self.provider = None

        def register_memory_provider(self, provider):
            self.provider = provider

    collector = Collector()
    module.register(collector)

    assert collector.provider is not None
    assert collector.provider.name == "cognee"


def test_missing_tool_returns_json_error():
    from cognee_integration_hermes import CogneeMemoryProvider

    provider = CogneeMemoryProvider()
    result = json.loads(provider.handle_tool_call("unknown", {}))
    assert "error" in result


def test_save_and_load_config(tmp_path, monkeypatch):
    from cognee_integration_hermes.config import load_config, save_config

    monkeypatch.delenv("COGNEE_SERVICE_URL", raising=False)
    monkeypatch.delenv("COGNEE_BASE_URL", raising=False)
    save_config({"dataset": "test_ds", "auto_route": False}, tmp_path)

    config = load_config(tmp_path)
    assert config["dataset"] == "test_ds"
    assert config["auto_route"] is False


def test_dataset_name_sanitization():
    from cognee_integration_hermes.config import sanitize_dataset_name

    assert sanitize_dataset_name("valid-Name_1.2") == "valid-Name_1.2"
    assert sanitize_dataset_name(" project/name!* ") == "project_name"
    assert sanitize_dataset_name("..__project__..") == "project"
    assert sanitize_dataset_name("a" * 130) == "a" * 120
    assert sanitize_dataset_name(" !!! ") == "hermes"


def test_load_config_normalizes_dataset(tmp_path, monkeypatch):
    from cognee_integration_hermes.config import load_config, save_config

    monkeypatch.delenv("COGNEE_SERVICE_URL", raising=False)
    monkeypatch.delenv("COGNEE_BASE_URL", raising=False)
    monkeypatch.delenv("COGNEE_DATASET", raising=False)
    save_config({"dataset": " file/dataset! "}, tmp_path)
    assert load_config(tmp_path)["dataset"] == "file_dataset"

    env_only_home = tmp_path / "env_only"
    env_only_home.mkdir()
    monkeypatch.setenv("COGNEE_DATASET", " env/dataset! ")
    assert load_config(env_only_home)["dataset"] == "env_dataset"
    assert load_config(tmp_path)["dataset"] == "file_dataset"


def test_empty_service_url_clears_remote_mode(tmp_path, monkeypatch):
    from cognee_integration_hermes.config import load_config, save_config

    monkeypatch.setenv("COGNEE_SERVICE_URL", "https://remote.example")
    save_config({"service_url": ""}, tmp_path)

    assert load_config(tmp_path)["service_url"] == ""
