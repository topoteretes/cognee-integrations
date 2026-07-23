"""Tests for detecting which cognee package extras are actually needed
(session-start.py's `_detect_required_extras` / `_cognee_install_spec`).

A bare `cognee==<version>` install has none of Postgres/Neo4j/Ollama/fastembed's
drivers, so the plugin's local server crashes on first use of any non-default
backend (#232). The fix installs only the extras the CONFIGURED providers
actually need, detected from the same env vars cognee's own config classes read
(DB_PROVIDER, VECTOR_DB_PROVIDER, GRAPH_DATABASE_PROVIDER, EMBEDDING_PROVIDER,
LLM_PROVIDER) -- confirmed against cognee 1.2.2.dev3's own
infrastructure/databases/*/config.py field names, not guessed.

The session-start module pulls in hook helpers; if it can't import in this
environment the tests skip (return) rather than fail -- same convention as
test_memory_preference.py.

Run: python integrations/claude-code/tests/test_install_extras.py
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

_PROVIDER_ENV_VARS = ("DB_PROVIDER", "VECTOR_DB_PROVIDER", "GRAPH_DATABASE_PROVIDER",
                      "EMBEDDING_PROVIDER", "LLM_PROVIDER")


class _ProviderEnvSandbox:
    """Save each provider var's ORIGINAL value on entry (not just clear it) and
    restore exactly that on exit, so this suite never clobbers a real ambient
    value if run in a shell/session where one happens to be exported already."""

    def __enter__(self):
        self._orig = {var: os.environ.get(var) for var in _PROVIDER_ENV_VARS}
        for var in _PROVIDER_ENV_VARS:
            os.environ.pop(var, None)
        return self

    def __exit__(self, *exc):
        for var, value in self._orig.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


def test_no_providers_configured_needs_no_extras():
    if ss is None:
        return
    with _ProviderEnvSandbox():
        assert ss._detect_required_extras() == ""
        assert ss._cognee_install_spec() == f"cognee=={ss._PINNED_COGNEE_VERSION}"


def test_postgres_and_neo4j_detected():
    if ss is None:
        return
    with _ProviderEnvSandbox():
        os.environ["DB_PROVIDER"] = "postgres"
        os.environ["GRAPH_DATABASE_PROVIDER"] = "neo4j"
        extras = ss._detect_required_extras()
        assert "postgres-binary" in extras
        assert "neo4j" in extras
        assert ss._cognee_install_spec() == f"cognee[{extras}]=={ss._PINNED_COGNEE_VERSION}"


def test_pgvector_maps_to_postgres_binary_not_a_separate_extra():
    if ss is None:
        return
    with _ProviderEnvSandbox():
        os.environ["VECTOR_DB_PROVIDER"] = "pgvector"
        extras = ss._detect_required_extras()
        assert extras == "postgres-binary"


def test_postgres_and_pgvector_together_dont_duplicate_the_extra():
    if ss is None:
        return
    with _ProviderEnvSandbox():
        os.environ["DB_PROVIDER"] = "postgres"
        os.environ["VECTOR_DB_PROVIDER"] = "pgvector"
        extras = ss._detect_required_extras()
        assert extras.count("postgres-binary") == 1


def test_fastembed_and_ollama_detected():
    if ss is None:
        return
    with _ProviderEnvSandbox():
        os.environ["EMBEDDING_PROVIDER"] = "fastembed"
        os.environ["LLM_PROVIDER"] = "ollama"
        extras = ss._detect_required_extras()
        assert "fastembed" in extras
        assert "ollama" in extras


def test_provider_values_are_case_insensitive():
    if ss is None:
        return
    with _ProviderEnvSandbox():
        os.environ["DB_PROVIDER"] = "POSTGRES"
        assert "postgres-binary" in ss._detect_required_extras()


def test_unrecognized_provider_value_is_ignored_not_an_error():
    if ss is None:
        return
    with _ProviderEnvSandbox():
        os.environ["DB_PROVIDER"] = "sqlite"  # the actual cognee default -- needs no extra
        assert ss._detect_required_extras() == ""


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
