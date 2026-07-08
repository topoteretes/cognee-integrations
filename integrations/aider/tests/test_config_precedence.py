import json

from cognee_integration_aider.config import load_config


def test_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("AIDER_COGNEE_CONFIG", raising=False)
    monkeypatch.delenv("COGNEE_DATASET", raising=False)

    config = load_config(tmp_path)

    assert config.dataset == "aider"
    assert config.session_prefix == "aider"
    assert config.top_k == 5
    assert config.self_improvement is False


def test_file_config(monkeypatch, tmp_path):
    config_path = tmp_path / "cognee.json"
    config_path.write_text(
        json.dumps({"dataset": "file-dataset", "top_k": 7, "self_improvement": True}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIDER_COGNEE_CONFIG", str(config_path))
    monkeypatch.delenv("COGNEE_DATASET", raising=False)

    config = load_config(tmp_path)

    assert config.dataset == "file-dataset"
    assert config.top_k == 7
    assert config.self_improvement is True


def test_env_wins_over_file(monkeypatch, tmp_path):
    config_path = tmp_path / "cognee.json"
    config_path.write_text(json.dumps({"dataset": "file-dataset", "top_k": 7}), encoding="utf-8")
    monkeypatch.setenv("AIDER_COGNEE_CONFIG", str(config_path))
    monkeypatch.setenv("COGNEE_DATASET", "env-dataset")
    monkeypatch.setenv("COGNEE_TOP_K", "11")

    config = load_config(tmp_path)

    assert config.dataset == "env-dataset"
    assert config.top_k == 11
