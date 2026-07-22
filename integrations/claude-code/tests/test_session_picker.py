"""Tests for the project-level ``.cognee/session-config.json`` dataset picker.

The picker lets a repo pin its Cognee dataset (and other non-secret selection
keys) via ``.cognee/session-config.json``, resolved at ``SessionStart``. On
current ``main`` the effective dataset precedence is ``env > picker > default``
(the global ``config.json`` deliberately does not drive the dataset). Secrets
and backend-routing keys in the picker file are never honored.

Fixture-free (like the sibling claude-code tests) so it runs standalone under
plain ``python3`` as well as ``pytest``.

Run: python integrations/claude-code/tests/test_session_picker.py (or via pytest).
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import config  # noqa: E402

# Every env var load_config reads (plus CLAUDE_CWD), cleared per test so a value
# in the developer's shell (e.g. a real LLM_API_KEY) can't leak in and make a
# test non-deterministic.
_MANAGED_ENV = set(config._ENV_MAP) | {"CLAUDE_CWD"}
_MANAGED_ATTRS = ("_CONFIG_DIR", "_STATE_DIR", "_CONFIG_FILE", "_HOOK_LOG")


@contextlib.contextmanager
def _isolated():
    """Redirect config's home/state dirs into a temp tree and clear its env vars.

    Yields ``(home, project)`` temp paths and restores config module globals,
    the environment, and cwd on exit — so tests never touch the real
    ``~/.cognee-plugin`` (config logging included).
    """
    saved_env = {k: os.environ.get(k) for k in _MANAGED_ENV}
    saved_attrs = {k: getattr(config, k) for k in _MANAGED_ATTRS}
    saved_cwd = os.getcwd()
    tmp = tempfile.mkdtemp()
    try:
        for k in _MANAGED_ENV:
            os.environ.pop(k, None)
        home = Path(tmp) / "home" / ".cognee-plugin" / "claude-code"
        home.mkdir(parents=True)
        config._CONFIG_DIR = home
        config._STATE_DIR = home
        config._CONFIG_FILE = home / "config.json"
        config._HOOK_LOG = home / "hook.log"
        project = Path(tmp) / "project"
        project.mkdir()
        yield home, project
    finally:
        os.chdir(saved_cwd)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in saved_attrs.items():
            setattr(config, k, v)
        shutil.rmtree(tmp, ignore_errors=True)


def _write_picker(project: Path, data) -> None:
    (project / ".cognee").mkdir(parents=True, exist_ok=True)
    body = data if isinstance(data, str) else json.dumps(data)
    (project / ".cognee" / "session-config.json").write_text(body, encoding="utf-8")


def _write_global_config(home: Path, data: dict) -> None:
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")


# --- resolution of the project root -----------------------------------------


def test_picker_resolves_via_explicit_cwd_arg():
    with _isolated() as (_home, project):
        _write_picker(project, {"dataset": "picker-dataset"})
        assert config.load_config(cwd=str(project)).get("dataset") == "picker-dataset"


def test_picker_resolves_via_claude_cwd_env_fallback():
    with _isolated() as (_home, project):
        _write_picker(project, {"dataset": "picker-env-dataset"})
        os.environ["CLAUDE_CWD"] = str(project)  # preferred over os.getcwd()
        assert config.load_config().get("dataset") == "picker-env-dataset"


def test_picker_falls_back_to_os_getcwd_last():
    with _isolated() as (_home, project):
        _write_picker(project, {"dataset": "picker-cwd-dataset"})
        os.chdir(project)
        assert config.load_config().get("dataset") == "picker-cwd-dataset"


# --- precedence --------------------------------------------------------------


def test_precedence_env_beats_picker():
    with _isolated() as (_home, project):
        _write_picker(project, {"dataset": "picker-dataset"})
        os.environ["COGNEE_PLUGIN_DATASET"] = "env-dataset"
        assert config.load_config(cwd=str(project)).get("dataset") == "env-dataset"


def test_precedence_picker_beats_default():
    with _isolated() as (_home, project):
        _write_picker(project, {"dataset": "picker-dataset"})
        assert config.load_config(cwd=str(project)).get("dataset") == "picker-dataset"


def test_global_config_dataset_is_visibility_only():
    # main excludes `dataset` from the global config file layer (visibility-only),
    # so a global config.json dataset does NOT drive the runtime dataset.
    with _isolated() as (home, project):
        _write_global_config(home, {"dataset": "global-dataset"})
        assert config.load_config(cwd=str(project)).get("dataset") == "agent_sessions"


# --- fail-safe fallthrough ---------------------------------------------------


def test_missing_picker_file_falls_through_to_default():
    with _isolated() as (_home, project):
        assert config.load_config(cwd=str(project)).get("dataset") == "agent_sessions"


def test_malformed_json_picker_falls_through():
    with _isolated() as (_home, project):
        _write_picker(project, "{invalid_json:")
        assert config.load_config(cwd=str(project)).get("dataset") == "agent_sessions"


def test_null_dataset_value_falls_through():
    with _isolated() as (_home, project):
        _write_picker(project, {"dataset": None})
        assert config.load_config(cwd=str(project)).get("dataset") == "agent_sessions"


def test_empty_string_dataset_falls_through():
    with _isolated() as (_home, project):
        _write_picker(project, {"dataset": ""})
        assert config.load_config(cwd=str(project)).get("dataset") == "agent_sessions"


def test_non_dict_picker_falls_through():
    with _isolated() as (_home, project):
        _write_picker(project, ["list", "instead", "of", "dict"])
        assert config.load_config(cwd=str(project)).get("dataset") == "agent_sessions"


# --- allowlist ---------------------------------------------------------------


def test_picker_ignores_sensitive_keys():
    # A repo-committed picker file must not redirect the backend or inject
    # credentials — only non-secret selection keys are honored.
    with _isolated() as (_home, project):
        _write_picker(
            project,
            {
                "dataset": "picker-dataset",
                "base_url": "http://evil.example",
                "api_key": "ck_stolen",
                "llm_api_key": "sk-stolen",
                "backend": "server",
            },
        )
        cfg = config.load_config(cwd=str(project))
        assert cfg.get("dataset") == "picker-dataset"  # allowlisted key applied
        assert cfg.get("base_url") == ""  # sensitive keys ignored (defaults)
        assert cfg.get("api_key") == ""
        assert cfg.get("llm_api_key") == ""
        assert cfg.get("backend") == "auto"


def test_picker_honors_allowlisted_nondataset_key():
    with _isolated() as (_home, project):
        _write_picker(project, {"session_strategy": "git-branch"})
        assert config.load_config(cwd=str(project)).get("session_strategy") == "git-branch"


def test_picker_unknown_key_ignored():
    with _isolated() as (_home, project):
        _write_picker(project, {"dataset": "picker-dataset", "totally_unknown": "x"})
        cfg = config.load_config(cwd=str(project))
        assert cfg.get("dataset") == "picker-dataset"
        assert "totally_unknown" not in cfg


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
