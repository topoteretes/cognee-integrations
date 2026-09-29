"""Tests for env-file parsing and settings resolution."""

from cognee_band import config
from cognee_band.config import CogneeSettings, parse_env_file


def test_parse_env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n"
        'COGNEE_BASE_URL="https://x.cognee.ai"\n'
        "export COGNEE_API_KEY=ck_1\n"
        "PATH=/evil\n"
        "malformed line\n"
        "COGNEE_API_KEY=ck_2\n"
    )
    values = parse_env_file(f)
    assert values["COGNEE_BASE_URL"] == "https://x.cognee.ai"
    # last value wins
    assert values["COGNEE_API_KEY"] == "ck_2"
    # parse keeps PATH; the loader blocks it
    assert config._blocked("PATH")


def test_resolve_from_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_loaded", True)  # skip file load
    monkeypatch.setenv("COGNEE_BASE_URL", "https://srv.test")
    monkeypatch.setenv("COGNEE_API_KEY", "ck_abc")
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "my-ds")
    monkeypatch.setenv("COGNEE_RECALL_TOP_K", "7")
    s = CogneeSettings.resolve()
    assert s.base_url == "https://srv.test"
    assert s.api_key == "ck_abc"
    assert s.dataset == "my-ds"
    assert s.top_k == 7


def test_resolve_defaults(monkeypatch):
    monkeypatch.setattr(config, "_loaded", True)
    for var in (
        "COGNEE_BASE_URL",
        "COGNEE_API_KEY",
        "COGNEE_PLUGIN_DATASET",
        "COGNEE_RECALL_TOP_K",
    ):
        monkeypatch.delenv(var, raising=False)
    s = CogneeSettings.resolve()
    assert s.base_url == "http://localhost:8011"
    assert s.dataset == "agent_sessions"
    assert s.top_k == 5


def test_overrides_beat_env(monkeypatch):
    monkeypatch.setattr(config, "_loaded", True)
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "env-ds")
    s = CogneeSettings.resolve(dataset="override-ds")
    assert s.dataset == "override-ds"


def test_session_id_for_room():
    s = CogneeSettings()
    assert s.session_id_for_room("room-42") == "band-room-42"
