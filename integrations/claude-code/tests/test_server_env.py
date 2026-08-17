"""Tests for `_build_server_env` (session-start.py) -- the spawned local Cognee
server's environment.

The server runs as a SEPARATE process from the hook that spawns it, so it only
sees what's explicitly placed in its own environment. LLM credentials that came
from the config FILE (not a live env var) are applied in-process elsewhere
(ensure_cognee_ready's cognee.config.set_llm_api_key/set_llm_model) and are
never written back to the config file (save_config's "transient_keys"), so a
plain os.environ.copy() alone leaves the server with no LLM key -- every
extraction then fails with LLMAPIKeyNotSetError, writes still report success
(the raw document is stored) but nothing lands in the graph, and recall keeps
returning empty even though nothing looked broken from the outside.

The session-start module pulls in hook helpers; if it can't import in this
environment the tests skip (return) rather than fail -- same convention as
test_memory_preference.py.

Run: python integrations/claude-code/tests/test_server_env.py
(or via pytest).
"""

import importlib.util
import os
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location(
        "session_start_mod", _SCRIPTS / "session-start.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    ss = _load()
except Exception:  # pragma: no cover - hook deps not importable in this environment
    ss = None


class _EnvVar:
    """Save/restore a single env var's ORIGINAL value (not just pop-to-absent),
    so this suite never clobbers a real ambient value (e.g. a contributor's own
    exported LLM_API_KEY) if run in the same shell/session as something that
    depends on it."""

    def __init__(self, name, value):
        self.name = name
        self.value = value
        self._had = name in os.environ
        self._orig = os.environ.get(name)

    def __enter__(self):
        if self.value is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.value
        return self

    def __exit__(self, *exc):
        if self._had:
            os.environ[self.name] = self._orig
        else:
            os.environ.pop(self.name, None)


def test_llm_api_key_from_config_reaches_server_env():
    if ss is None:
        return
    with _EnvVar("LLM_API_KEY", None):
        env = ss._build_server_env({"llm_api_key": "sk-from-config-file", "llm_model": ""})
        assert env["LLM_API_KEY"] == "sk-from-config-file"


def test_llm_model_from_config_reaches_server_env():
    if ss is None:
        return
    with _EnvVar("LLM_MODEL", None):
        env = ss._build_server_env({"llm_api_key": "", "llm_model": "gpt-4o-mini"})
        assert env["LLM_MODEL"] == "gpt-4o-mini"


def test_explicit_env_var_always_wins_over_config():
    if ss is None:
        return
    with _EnvVar("LLM_API_KEY", "sk-from-live-env"):
        env = ss._build_server_env({"llm_api_key": "sk-from-config-file", "llm_model": ""})
        assert env["LLM_API_KEY"] == "sk-from-live-env"


def test_missing_config_values_dont_set_empty_env_vars():
    if ss is None:
        return
    with _EnvVar("LLM_API_KEY", None), _EnvVar("LLM_MODEL", None):
        env = ss._build_server_env({"llm_api_key": "", "llm_model": ""})
        assert "LLM_API_KEY" not in env
        assert "LLM_MODEL" not in env


def test_agent_mode_is_always_set():
    if ss is None:
        return
    env = ss._build_server_env({})
    assert env["COGNEE_AGENT_MODE"] == "true"


def test_existing_environ_is_carried_through():
    if ss is None:
        return
    with _EnvVar("COGNEE_TEST_MARKER_XYZ", "present"):
        env = ss._build_server_env({})
        assert env.get("COGNEE_TEST_MARKER_XYZ") == "present"


if __name__ == "__main__":
    if ss is None:
        print("SKIP: session-start.py not importable in this environment")
        sys.exit(0)
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
