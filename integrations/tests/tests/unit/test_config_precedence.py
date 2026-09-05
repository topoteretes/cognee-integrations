"""Current env/default and active-launch precedence, replacing PR #169."""

import json


def test_shell_beats_dotenv_and_legacy_json(suite, isolated_modules, monkeypatch, tmp_path):
    config = isolated_modules(suite, "config")
    ef = isolated_modules(suite, "_env_file")
    env = tmp_path / "settings.env"
    env.write_text("COGNEE_PLUGIN_DATASET=from-file\nCOGNEE_BASE_URL=https://file.example\n")
    monkeypatch.setenv("COGNEE_ENV_FILE", str(env))
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "from-shell")
    monkeypatch.setattr(ef, "_loaded", False)
    ef.load_env_file()
    config._STATE_DIR.mkdir(parents=True, exist_ok=True)
    (config._STATE_DIR / "config.json").write_text(json.dumps({"dataset": "obsolete"}))
    cfg = config.load_config()
    assert cfg["dataset"] == "from-shell"
    assert cfg["base_url"] == "https://file.example"


def test_plugin_specific_backend_wins(suite, isolated_modules, monkeypatch):
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_BACKEND", "cloud")
    target = {"claude-code": "CLAUDE", "codex": "CODEX", "antigravity": "ANTIGRAVITY"}[suite.name]
    monkeypatch.setenv("COGNEE_" + target + "_BACKEND", "local")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://cloud.example")
    cfg = config.load_config()
    assert cfg["_forced_backend"] == "local"
    assert cfg["base_url"] == ""


def test_empty_overrides_use_defaults(suite, isolated_modules, monkeypatch):
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "")
    assert config.load_config()["dataset"] == "agent_sessions"
