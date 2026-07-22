"""Unit tests for the Cognee Doctor command (Claude Code plugin).

Tests verify behaviour with mocked HTTP and filesystem state, covering:
  - local vs server mode resolution
  - reachable vs unreachable health endpoint
  - env vs config API key source
  - local (venv) / server cognee version + embedding fields
  - breaker state reporting
  - JSON output format

Every test runs under plain `python3 tests/test_doctor.py` (no pytest
fixtures) as well as under `pytest`, matching the sibling test convention.
"""

import json
import os
import pathlib
import sys
import tempfile
import urllib.error

_SCRIPTS_DIR = str(pathlib.Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

_TMP = tempfile.mkdtemp(prefix="cognee-doctor-test-")
os.environ["COGNEE_PLUGIN_STATE_DIR"] = _TMP

import _plugin_common  # noqa: E402
import doctor  # noqa: E402


def _reset_env(*keys):
    """Remove env vars that affect config resolution."""
    for key in keys:
        os.environ.pop(key, None)


def _reset_breaker():
    p = pathlib.Path(_TMP) / "recall-breaker.json"
    if p.exists():
        p.unlink()


class _FakeResponse:
    """Minimal stand-in for a urllib.request.urlopen response."""

    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _patch_urlopen:
    """Context manager that swaps urllib.request.urlopen for a stub."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def __enter__(self):
        import urllib.request

        self._mod = urllib.request
        self._orig = urllib.request.urlopen

        def _fake(*a, **k):
            if self._error is not None:
                raise self._error
            return self._response

        urllib.request.urlopen = _fake

    def __exit__(self, *a):
        self._mod.urlopen = self._orig


# Mode resolution


def test_local_mode_when_no_base_url():
    _reset_env("COGNEE_BASE_URL", "COGNEE_LOCAL_API_URL", "COGNEE_API_KEY", "LLM_API_KEY")
    assert doctor._resolve_mode() == "Local"


def test_managed_mode_with_localhost():
    os.environ["COGNEE_BASE_URL"] = "http://localhost:8011"
    try:
        assert doctor._resolve_mode() == "Local Managed"
    finally:
        _reset_env("COGNEE_BASE_URL")


def test_managed_mode_with_127():
    os.environ["COGNEE_BASE_URL"] = "http://127.0.0.1:8000"
    try:
        assert doctor._resolve_mode() == "Local Managed"
    finally:
        _reset_env("COGNEE_BASE_URL")


def test_managed_mode_with_ipv6_loopback():
    os.environ["COGNEE_BASE_URL"] = "http://[::1]:8000"
    try:
        assert doctor._resolve_mode() == "Local Managed"
    finally:
        _reset_env("COGNEE_BASE_URL")


def test_cloud_mode_with_remote_url():
    os.environ["COGNEE_BASE_URL"] = "https://company.cognee.ai"
    try:
        assert doctor._resolve_mode() == "Cloud"
    finally:
        _reset_env("COGNEE_BASE_URL")


# Server URL display


def test_server_url_dash_in_local_mode():
    _reset_env("COGNEE_BASE_URL", "COGNEE_LOCAL_API_URL", "LLM_API_KEY")
    display, _raw = doctor._resolve_server_url()
    assert display == "-"


def test_server_url_shown_in_server_mode():
    os.environ["COGNEE_BASE_URL"] = "http://custom:9999"
    try:
        _display, raw = doctor._resolve_server_url()
        assert "custom:9999" in raw
    finally:
        _reset_env("COGNEE_BASE_URL")


# API key source


def test_api_key_source_env():
    os.environ["COGNEE_API_KEY"] = "test-key-from-env"
    try:
        assert doctor._resolve_api_key_source() == "ENV"
    finally:
        _reset_env("COGNEE_API_KEY")


def test_api_key_source_config():
    # No env key; a cached key file should resolve to "Config".
    _reset_env("COGNEE_API_KEY", "COGNEE_BASE_URL", "COGNEE_LOCAL_API_URL")
    original = _plugin_common._API_KEY_CACHE
    cache_file = pathlib.Path(_TMP) / "api_key.json"
    cache_file.write_text(json.dumps({"api_key": "cached-key", "base_url": ""}))
    _plugin_common._API_KEY_CACHE = cache_file
    try:
        assert doctor._resolve_api_key_source() == "Config"
    finally:
        _plugin_common._API_KEY_CACHE = original
        _reset_env("COGNEE_API_KEY")


# Health check


def test_health_reachable():
    body = json.dumps({"status": "ready"}).encode()
    with _patch_urlopen(response=_FakeResponse(200, body)):
        result = doctor._check_health("http://fake:8011")
    assert result["reachable"] is True
    assert result["latency_ms"] is not None and result["latency_ms"] >= 0


def test_health_unreachable():
    with _patch_urlopen(error=urllib.error.URLError("connection refused")):
        result = doctor._check_health("http://fake:8011")
    assert result["reachable"] is False
    assert result["latency_ms"] is None


# Cognee versions


def test_server_version_unknown_when_not_in_body():
    assert doctor._resolve_server_version({"status": "ready"}) == "Unknown"
    assert doctor._resolve_server_version(None) == "Unknown"


def test_server_version_extracted_when_present():
    assert doctor._resolve_server_version({"version": "1.2.3"}) == "1.2.3"


def test_local_cognee_not_installed_when_venv_absent():
    original = _plugin_common._VENV_PYTHON
    _plugin_common._VENV_PYTHON = pathlib.Path(_TMP) / "no-such-venv" / "python"
    try:
        assert doctor._resolve_local_cognee_version() == "Not installed"
    finally:
        _plugin_common._VENV_PYTHON = original


# Embedding


def test_embedding_default_when_unset():
    _reset_env("EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS")
    assert doctor._resolve_embedding() == ("Default", "Default")


def test_embedding_from_env():
    os.environ["EMBEDDING_MODEL"] = "openai/text-embedding-3-large"
    os.environ["EMBEDDING_DIMENSIONS"] = "3072"
    try:
        assert doctor._resolve_embedding() == ("openai/text-embedding-3-large", "3072")
    finally:
        _reset_env("EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS")


# Circuit breaker


def test_breaker_closed():
    _reset_breaker()
    assert doctor._resolve_circuit_breaker() == "Closed"


def test_breaker_open():
    import time as _time

    breaker_path = pathlib.Path(_TMP) / "recall-breaker.json"
    breaker_path.write_text(
        json.dumps({"failures": 10, "cooldown_until": _time.time() + 60}),
        encoding="utf-8",
    )
    try:
        assert doctor._resolve_circuit_breaker().startswith("Open")
    finally:
        _reset_breaker()


# Output


def test_json_output():
    _reset_env("COGNEE_BASE_URL", "COGNEE_LOCAL_API_URL", "LLM_API_KEY")
    _reset_breaker()
    body = json.dumps({"status": "ready"}).encode()
    with _patch_urlopen(response=_FakeResponse(200, body)):
        report = doctor.collect_report()
    parsed = json.loads(doctor.format_json(report))
    expected = {
        "mode",
        "server_url",
        "api_key_source",
        "reachable",
        "latency_ms",
        "cognee_local",
        "cognee_server",
        "embedding_model",
        "embedding_dimensions",
        "circuit_breaker",
    }
    assert expected == set(parsed.keys()), f"keys mismatch: {expected ^ set(parsed.keys())}"


def test_human_output_contains_header():
    _reset_breaker()
    report = doctor.collect_report()
    text = doctor.format_human(report)
    assert "Cognee Doctor" in text
    assert "Mode:" in text
    assert "Cognee (local):" in text
    assert "Circuit Breaker:" in text


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            _fn()
            print(f"PASS {_name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {_name}: {e}")
    sys.exit(1 if failures else 0)
