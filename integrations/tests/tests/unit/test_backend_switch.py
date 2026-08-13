"""The COGNEE_BACKEND terminal switch (plus the per-plugin variants).

Users keep BOTH modes' variables in ~/.cognee/.env; with nothing exported the
cloud vars win (COGNEE_BASE_URL routes the connection). One export flips a
single terminal:

    export COGNEE_BACKEND=local    # or cloud

Forced local must hold everywhere — not just in load_config()'s view, but in
os.environ itself, because the HTTP hot paths (_plugin_common) and spawned
children read COGNEE_BASE_URL directly, and the scrub must survive a child
re-running the loader. Forced cloud is pinned even when the connection vars
are missing: is_cloud_mode() stays true, the plugin attempts the cloud
connection, and the status line reports ✕ (missing_cognee_base_url) instead
of silently falling back to local.
"""

from __future__ import annotations

import os

import pytest

#: Each suite's own switch, and the OTHER plugin's switch — which it must ignore.
OWN_BACKEND_VAR = {"claude-code": "COGNEE_CLAUDE_BACKEND", "codex": "COGNEE_CODEX_BACKEND"}
OTHER_BACKEND_VAR = {"claude-code": "COGNEE_CODEX_BACKEND", "codex": "COGNEE_CLAUDE_BACKEND"}

CLOUD_URL = "https://tenant.cognee.ai"
CLOUD_ENV_FILE = "\n".join(
    [
        f'COGNEE_BASE_URL="{CLOUD_URL}"',
        'COGNEE_API_KEY="ck_from_file"',
        'LLM_API_KEY="sk_from_file"',
        "",
    ]
)


@pytest.fixture
def env_file(suite, isolated_modules, tmp_path, monkeypatch):
    """The isolated _env_file module + a loader that runs a fresh env file."""
    ef = isolated_modules(suite, "_env_file")

    def _load(content: str):
        path = tmp_path / ".env"
        path.write_text(content, encoding="utf-8")
        monkeypatch.setenv("COGNEE_ENV_FILE", str(path))
        monkeypatch.setattr(ef, "_loaded", False)
        ef.load_env_file()

    return ef, _load


# ── environment layer (_env_file) ──────────────────────────────────────────


def test_no_switch_cloud_vars_win(env_file):
    ef, load = env_file
    load(CLOUD_ENV_FILE)
    assert os.environ["COGNEE_BASE_URL"] == CLOUD_URL
    assert os.environ["COGNEE_API_KEY"] == "ck_from_file"


def test_forced_local_scrubs_cloud_vars_from_environ(env_file, monkeypatch):
    ef, load = env_file
    monkeypatch.setenv("COGNEE_BACKEND", "local")
    load(CLOUD_ENV_FILE)
    assert os.environ["COGNEE_BASE_URL"] == ""
    assert os.environ["COGNEE_API_KEY"] == ""
    # Local-mode vars from the same file are untouched.
    assert os.environ["LLM_API_KEY"] == "sk_from_file"


def test_forced_local_beats_a_real_shell_export(env_file, monkeypatch):
    """The switch wins even over an exported URL, with no env file at all."""
    ef, _load = env_file
    monkeypatch.setenv("COGNEE_BASE_URL", CLOUD_URL)
    monkeypatch.setenv("COGNEE_BACKEND", "local")
    monkeypatch.setattr(ef, "_loaded", False)
    ef.load_env_file()
    assert os.environ["COGNEE_BASE_URL"] == ""


def test_scrub_survives_a_child_rerunning_the_loader(env_file, monkeypatch):
    """The scrub writes EMPTY strings, not deletions: a child re-running the
    loader must not re-inject the file's cloud URL (setdefault skips keys that
    are present, even when empty)."""
    ef, load = env_file
    monkeypatch.setenv("COGNEE_BACKEND", "local")
    load(CLOUD_ENV_FILE)
    monkeypatch.delenv("COGNEE_BACKEND")  # even if the child lost the switch
    monkeypatch.setattr(ef, "_loaded", False)
    ef.load_env_file()
    assert os.environ["COGNEE_BASE_URL"] == ""
    assert os.environ["COGNEE_API_KEY"] == ""


def test_forced_cloud_scrubs_nothing(env_file, monkeypatch):
    ef, load = env_file
    monkeypatch.setenv("COGNEE_BACKEND", "cloud")
    load(CLOUD_ENV_FILE)
    assert os.environ["COGNEE_BASE_URL"] == CLOUD_URL
    assert os.environ["COGNEE_API_KEY"] == "ck_from_file"


def test_plugin_var_beats_the_shared_var(env_file, suite, monkeypatch):
    ef, _load = env_file
    monkeypatch.setenv("COGNEE_BACKEND", "local")
    monkeypatch.setenv(OWN_BACKEND_VAR[suite.name], "cloud")
    assert ef.forced_backend() == "cloud"


def test_the_other_plugins_var_is_ignored(env_file, suite, monkeypatch):
    ef, _load = env_file
    monkeypatch.setenv(OTHER_BACKEND_VAR[suite.name], "local")
    assert ef.forced_backend() == ""


def test_unrecognized_value_is_ignored(env_file, monkeypatch):
    ef, _load = env_file
    monkeypatch.setenv("COGNEE_BACKEND", "bananas")
    assert ef.forced_backend() == ""


@pytest.mark.parametrize("value", ["local", "native", "sdk", "LOCAL", " local "])
def test_local_synonyms_and_normalization(env_file, monkeypatch, value):
    ef, _load = env_file
    monkeypatch.setenv("COGNEE_BACKEND", value)
    assert ef.forced_backend() == "local"


@pytest.mark.parametrize("value", ["cloud", "http", "api", "server"])
def test_cloud_synonyms(env_file, monkeypatch, value):
    ef, _load = env_file
    monkeypatch.setenv("COGNEE_BACKEND", value)
    assert ef.forced_backend() == "cloud"


def test_template_documents_the_switch(env_file):
    ef, _load = env_file
    assert "COGNEE_BACKEND" in ef._TEMPLATE


# ── config layer (load_config / is_cloud_mode) ─────────────────────────────


@pytest.fixture
def config_mod(suite, isolated_modules):
    return isolated_modules(suite, "config")


def test_config_no_switch_cloud_wins(config_mod, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", CLOUD_URL)
    monkeypatch.setenv("LLM_API_KEY", "sk")
    cfg = config_mod.load_config()
    assert config_mod.is_cloud_mode(cfg)
    assert not config_mod.is_local_mode(cfg)


def test_config_forced_local_despite_cloud_vars(config_mod, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", CLOUD_URL)
    monkeypatch.setenv("COGNEE_API_KEY", "ck")
    monkeypatch.setenv("LLM_API_KEY", "sk")
    monkeypatch.setenv("COGNEE_BACKEND", "local")
    cfg = config_mod.load_config()
    assert not config_mod.is_cloud_mode(cfg)
    assert config_mod.is_local_mode(cfg)
    assert cfg["base_url"] == ""
    assert cfg["api_key"] == ""
    assert cfg["llm_api_key"] == "sk"


def test_config_forced_cloud_is_pinned_without_a_url(config_mod, monkeypatch):
    """Missing connection vars must NOT silently fall back to local."""
    monkeypatch.setenv("LLM_API_KEY", "sk")
    monkeypatch.setenv("COGNEE_BACKEND", "cloud")
    cfg = config_mod.load_config()
    assert config_mod.is_cloud_mode(cfg)
    assert not config_mod.is_local_mode(cfg)
    assert cfg["base_url"] == ""


def test_config_plugin_var_beats_shared(config_mod, suite, monkeypatch):
    monkeypatch.setenv("COGNEE_BACKEND", "cloud")
    monkeypatch.setenv(OWN_BACKEND_VAR[suite.name], "local")
    monkeypatch.setenv("COGNEE_BASE_URL", CLOUD_URL)
    cfg = config_mod.load_config()
    assert not config_mod.is_cloud_mode(cfg)


def test_config_ignores_the_other_plugins_var(config_mod, suite, monkeypatch):
    monkeypatch.setenv(OTHER_BACKEND_VAR[suite.name], "local")
    monkeypatch.setenv("COGNEE_BASE_URL", CLOUD_URL)
    cfg = config_mod.load_config()
    assert config_mod.is_cloud_mode(cfg)


# ── status line ─────────────────────────────────────────────────────────────


def test_statusline_forced_local_shows_local(statusline, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", CLOUD_URL)
    monkeypatch.setenv("COGNEE_BACKEND", "local")
    assert statusline._active_mode() == "local"


def test_statusline_forced_cloud_without_url_warns(statusline, monkeypatch):
    monkeypatch.setenv("COGNEE_BACKEND", "cloud")
    assert statusline._active_mode() == "cloud"
    prefix = statusline._status_prefix("sess-1")
    assert "missing_cognee_base_url" in prefix
    assert "✕" in prefix


def test_statusline_forced_cloud_with_url_does_not_warn(statusline, monkeypatch):
    monkeypatch.setenv("COGNEE_BACKEND", "cloud")
    monkeypatch.setenv("COGNEE_BASE_URL", CLOUD_URL)
    assert statusline._active_mode() == "cloud"
    assert "missing_cognee_base_url" not in statusline._status_prefix("sess-1")


def test_statusline_no_switch_keeps_url_heuristic(statusline, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", "http://localhost:8011")
    assert statusline._active_mode() == "local"


# ── doctor ──────────────────────────────────────────────────────────────────


@pytest.fixture
def doctor(suite, isolated_modules):
    return isolated_modules(suite, "doctor")


def test_doctor_reports_forced_cloud_without_url(doctor, monkeypatch):
    monkeypatch.setenv("COGNEE_BACKEND", "cloud")
    assert doctor._resolve_mode() == "Cloud"
    assert "missing COGNEE_BASE_URL" in doctor._mode_annotation()
    display, raw = doctor._resolve_server_url()
    assert display == "-"
    assert raw == ""


def test_doctor_reports_forced_local(doctor, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", CLOUD_URL)
    monkeypatch.setenv("COGNEE_BACKEND", "local")
    assert doctor._resolve_mode() == "Local"
    assert "COGNEE_BACKEND=local" in doctor._mode_annotation()
