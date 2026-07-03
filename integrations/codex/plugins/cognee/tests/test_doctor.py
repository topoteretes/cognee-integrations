"""Unit tests for the Cognee Doctor command (Codex plugin).

Tests verify behaviour with mocked HTTP and filesystem state, covering:
  - local vs server mode resolution
  - reachable vs unreachable health endpoint
  - env vs config API key source
  - breaker state reporting
  - JSON output format

Run:
    pytest integrations/codex/plugins/cognee/tests/test_doctor.py
    python integrations/codex/plugins/cognee/tests/test_doctor.py   # standalone
"""

import io
import json
import os
import pathlib
import sys
import tempfile
import textwrap
import urllib.error

_SCRIPTS_DIR = str(pathlib.Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

_TMP = tempfile.mkdtemp(prefix="cognee-doctor-codex-test-")
os.environ["COGNEE_PLUGIN_STATE_DIR"] = _TMP

# pyrefly: ignore [missing-import]
import doctor  

def _reset_env(*keys):
    """Remove env vars that affect config resolution."""
    for key in keys:
        os.environ.pop(key, None)


def _reset_breaker():
    p = pathlib.Path(_TMP) / "recall-breaker.json"
    if p.exists():
        p.unlink()


class _FakeResponse:
    """Minimal stand-in for urllib.request.urlopen response."""

    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_local_mode_when_no_base_url():
    _reset_env("COGNEE_BASE_URL", "COGNEE_LOCAL_API_URL", "COGNEE_API_KEY", "LLM_API_KEY")
    mode = doctor._resolve_mode()
    assert mode == "Local", f"expected Local, got {mode}"


def test_managed_mode_with_localhost():
    os.environ["COGNEE_BASE_URL"] = "http://localhost:8011"
    try:
        mode = doctor._resolve_mode()
        assert mode == "Local Managed", f"expected Local Managed, got {mode}"
    finally:
        _reset_env("COGNEE_BASE_URL")


def test_managed_mode_with_127():
    os.environ["COGNEE_BASE_URL"] = "http://127.0.0.1:8000"
    try:
        mode = doctor._resolve_mode()
        assert mode == "Local Managed", f"expected Local Managed, got {mode}"
    finally:
        _reset_env("COGNEE_BASE_URL")


def test_managed_mode_with_ipv6_loopback():
    os.environ["COGNEE_BASE_URL"] = "http://[::1]:8000"
    try:
        mode = doctor._resolve_mode()
        assert mode == "Local Managed", f"expected Local Managed, got {mode}"
    finally:
        _reset_env("COGNEE_BASE_URL")


def test_cloud_mode_with_remote_url():
    os.environ["COGNEE_BASE_URL"] = "https://company.cognee.ai"
    try:
        mode = doctor._resolve_mode()
        assert mode == "Cloud", f"expected Cloud, got {mode}"
    finally:
        _reset_env("COGNEE_BASE_URL")


# Server URL display

def test_server_url_dash_in_local_mode():
    _reset_env("COGNEE_BASE_URL", "COGNEE_LOCAL_API_URL", "LLM_API_KEY")
    display, _raw = doctor._resolve_server_url()
    assert display == "-", f"expected '-', got {display}"


def test_server_url_shown_in_server_mode():
    os.environ["COGNEE_BASE_URL"] = "http://custom:9999"
    try:
        display, raw = doctor._resolve_server_url()
        assert "custom:9999" in raw
    finally:
        _reset_env("COGNEE_BASE_URL")


# API key source

def test_api_key_source_env():
    os.environ["COGNEE_API_KEY"] = "test-key-from-env"
    try:
        source = doctor._resolve_api_key_source()
        assert source == "ENV", f"expected ENV, got {source}"
    finally:
        _reset_env("COGNEE_API_KEY")


def test_api_key_source_config(tmp_path):
    _reset_env("COGNEE_API_KEY")
    # Temporarily swap the cache path to a temp file with a cached key.
    original = doctor._API_KEY_CACHE
    cache_file = tmp_path / "api_key.json"
    cache_file.write_text(json.dumps({"api_key": "cached-key", "base_url": ""}))
    doctor._API_KEY_CACHE = cache_file
    try:
        source = doctor._resolve_api_key_source()
        assert source == "Config", f"expected Config, got {source}"
    finally:
        doctor._API_KEY_CACHE = original


# Health check

def test_health_reachable(monkeypatch):
    body = json.dumps({"status": "ok"}).encode()
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(200, body),
    )
    result = doctor._check_health("http://fake:8011")
    assert result["reachable"] is True
    assert result["latency_ms"] is not None and result["latency_ms"] >= 0


def test_health_unreachable(monkeypatch):
    def _raise(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    result = doctor._check_health("http://fake:8011")
    assert result["reachable"] is False
    assert result["latency_ms"] is None


# Server version

def test_server_version_unknown_when_not_in_body():
    assert doctor._resolve_server_version({"status": "ok"}) == "Unknown"
    assert doctor._resolve_server_version(None) == "Unknown"


def test_server_version_extracted_when_present():
    assert doctor._resolve_server_version({"version": "1.2.3"}) == "1.2.3"


# Circuit breaker

def test_breaker_closed():
    _reset_breaker()
    state = doctor._resolve_circuit_breaker()
    assert state == "Closed", f"expected Closed, got {state}"


def test_breaker_open():
    import time as _time

    breaker_path = pathlib.Path(_TMP) / "recall-breaker.json"
    breaker_path.write_text(
        json.dumps({"failures": 10, "cooldown_until": _time.time() + 60}),
        encoding="utf-8",
    )
    state = doctor._resolve_circuit_breaker()
    assert state.startswith("Open"), f"expected Open..., got {state}"
    _reset_breaker()


# Plugin version (inventory lookup)

def test_parse_inventory_version(tmp_path):
    inventory = tmp_path / "inventory.yml"
    inventory.write_text(
        textwrap.dedent("""\
        integrations:
          - slug: claude-code
            current_version: "0.1.0"
          - slug: codex
            current_version: "1.0.3-local"
        """),
        encoding="utf-8",
    )
    assert doctor._parse_inventory_version(inventory, "codex") == "1.0.3-local"
    assert doctor._parse_inventory_version(inventory, "claude-code") == "0.1.0"
    assert doctor._parse_inventory_version(inventory, "nonexistent") == "Unknown"


# JSON output

def test_json_output(monkeypatch):
    body = json.dumps({"status": "ok"}).encode()
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(200, body),
    )
    _reset_env("COGNEE_BASE_URL", "COGNEE_LOCAL_API_URL", "LLM_API_KEY")
    _reset_breaker()

    report = doctor.collect_report()
    output = doctor.format_json(report)
    parsed = json.loads(output)

    expected_keys = {
        "mode", "server_url", "api_key_source", "reachable", "latency_ms",
        "plugin_version", "server_version", "embedding_model",
        "embedding_dimensions", "circuit_breaker",
    }
    assert expected_keys == set(parsed.keys()), f"missing keys: {expected_keys - set(parsed.keys())}"


def test_human_output_contains_header():
    _reset_breaker()
    report = doctor.collect_report()
    text = doctor.format_human(report)
    assert "Cognee Doctor" in text
    assert "Mode:" in text
    assert "Circuit Breaker:" in text


if __name__ == "__main__":
    failures = 0
    skipped = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue

        import inspect

        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())

        if "monkeypatch" in params or "tmp_path" in params:
            skipped += 1
            print(f"SKIP {name} (requires pytest fixtures)")
            continue

        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
    if skipped:
        print(f"\n{skipped} test(s) skipped (run with pytest for full coverage)")
    sys.exit(1 if failures else 0)
