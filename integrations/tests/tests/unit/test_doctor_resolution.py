"""What `cognee-doctor` reports, for everything it resolves locally.

`doctor.py` is the first thing a user runs when memory "isn't working", so each
line has to be right about where the plugin is actually pointed: Local (no URL)
vs Local Managed (loopback URL) vs Cloud, which endpoint won, whether the API key
came from the environment or the cached file, the embedding settings, and the
circuit-breaker state.

The endpoint-resolution precedence (env > config file > localhost default) is
asserted for all registered suites here. The config-file layer is wired into the
Codex-derived cores, so those cases skip on Claude Code; the doctor modules keep
their own `_API_KEY_CACHE` constant, which the fixture accounts for.

The `/health` probe and the full report live in integration/test_doctor.py.
Migrated from claude-code/tests/test_doctor.py and
codex/plugins/cognee/tests/test_doctor.py, both now deleted: every case in
codex's copy has a counterpart here or in integration/test_doctor.py, and the
Windows workflow runs this suite rather than that script.
"""

from __future__ import annotations

import json
import time

import pytest


@pytest.fixture
def doctor(suite, isolated_modules):
    return isolated_modules(suite, "doctor")


@pytest.fixture
def pc(suite, isolated_modules):
    return isolated_modules(suite, "_plugin_common")


@pytest.fixture
def config_file(suite, isolated_modules, pc):
    """The suite's config.json path, or skip if its endpoint layer ignores it."""
    config = isolated_modules(suite, "config")
    url, source = pc._local_api_url_with_source()
    if source != "default_local":
        pytest.skip("unexpected ambient endpoint resolution")
    path = config._CONFIG_FILE

    def _write(payload: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    _write({"base_url": "http://probe-config-support:8012"})
    if pc._local_api_url_with_source()[1] != "config_base_url":
        pytest.skip(f"{suite.name}: the endpoint layer does not read config.json")
    path.unlink()
    return _write


# ── mode resolution ────────────────────────────────────────────────────────


def test_local_mode_when_no_base_url(doctor):
    """The harness scrubs COGNEE_*, so this is a genuinely unconfigured machine."""
    assert doctor._resolve_mode() == "Local"


@pytest.mark.parametrize(
    "url", ["http://localhost:8011", "http://127.0.0.1:8000", "http://[::1]:8000"]
)
def test_managed_mode_for_every_loopback_form(doctor, monkeypatch, url):
    monkeypatch.setenv("COGNEE_BASE_URL", url)
    assert doctor._resolve_mode() == "Local Managed"


def test_cloud_mode_with_remote_url(doctor, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", "https://company.cognee.ai")
    assert doctor._resolve_mode() == "Cloud"


# ── server URL display ─────────────────────────────────────────────────────


def test_server_url_dash_in_local_mode(doctor):
    display, _raw = doctor._resolve_server_url()
    assert display == "-"


def test_server_url_shown_in_server_mode(doctor, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", "http://custom:9999")
    _display, raw = doctor._resolve_server_url()
    assert "custom:9999" in raw


# ── endpoint precedence: env > config file > localhost default ─────────────


def test_server_url_falls_back_to_config_file(doctor, pc, config_file):
    configured = "http://managed-cognee.internal:8012"
    config_file({"base_url": configured})
    assert pc._local_api_url_with_source() == (configured, "config_base_url")
    assert doctor._resolve_server_url() == (configured, configured)


def test_endpoint_env_vars_keep_precedence_over_config(pc, config_file, monkeypatch):
    config_file({"backend": "http", "base_url": "http://from-config:8012"})
    monkeypatch.setenv("COGNEE_BASE_URL", "http://from-base-env:8013")
    assert pc._local_api_url_with_source() == ("http://from-base-env:8013", "env_service_url")

    monkeypatch.setenv("COGNEE_LOCAL_API_URL", "http://from-local-env:8014")
    assert pc._local_api_url_with_source() == ("http://from-local-env:8014", "env_local_api_url")


def test_local_backend_ignores_stale_config_endpoint(pc, config_file):
    """`backend: local` means local, whatever endpoint the file still remembers."""
    config_file({"backend": "local", "base_url": "http://stale-managed:8012"})
    assert pc._local_api_url_with_source() == ("http://localhost:8011", "default_local")


def test_malformed_config_falls_back_to_localhost(pc, config_file):
    path = config_file({})
    path.write_text("{not-json", encoding="utf-8")
    assert pc._local_api_url_with_source() == ("http://localhost:8011", "default_local")


# ── API key source ─────────────────────────────────────────────────────────


def test_api_key_source_env(doctor, monkeypatch):
    monkeypatch.setenv("COGNEE_API_KEY", "test-key-from-env")
    assert doctor._resolve_api_key_source() == "ENV"


def test_api_key_source_config(doctor, pc, tmp_path, monkeypatch):
    """No env key; a cached key file resolves to "Config"."""
    cache_file = tmp_path / "api_key.json"
    cache_file.write_text(json.dumps({"api_key": "cached-key", "base_url": ""}), encoding="utf-8")
    # codex's doctor.py defines its own _API_KEY_CACHE constant.
    owner = doctor if hasattr(doctor, "_API_KEY_CACHE") else pc
    monkeypatch.setattr(owner, "_API_KEY_CACHE", cache_file)
    assert doctor._resolve_api_key_source() == "Config"


def test_cached_api_key_remains_scoped_to_its_endpoint(pc, config_file, tmp_path, monkeypatch):
    """A key cached for another server must never be sent to this one."""
    configured = "http://managed-cognee.internal:8012"
    config_file({"base_url": configured})
    cache_file = tmp_path / "runtime-api-key.json"
    monkeypatch.setattr(pc, "_API_KEY_CACHE", cache_file)

    cache_file.write_text(
        json.dumps({"api_key": "matching-key", "base_url": configured}), encoding="utf-8"
    )
    assert pc.resolved_http_endpoint_auth() == (configured, "matching-key")

    # A successful resolution exports the key into the process env so sibling
    # hooks reuse it, so that has to be cleared before the mismatch case —
    # otherwise the env hit would mask what the cache file says.
    monkeypatch.delenv("COGNEE_API_KEY", raising=False)
    cache_file.write_text(
        json.dumps({"api_key": "wrong-key", "base_url": "http://other:8012"}), encoding="utf-8"
    )
    assert pc.resolved_http_endpoint_auth() == (configured, "")


# ── versions ───────────────────────────────────────────────────────────────


def test_server_version_unknown_when_not_in_body(doctor):
    assert doctor._resolve_server_version({"status": "ready"}) == "Unknown"
    assert doctor._resolve_server_version(None) == "Unknown"


def test_server_version_extracted_when_present(doctor):
    assert doctor._resolve_server_version({"version": "1.2.3"}) == "1.2.3"


def test_local_cognee_not_installed_when_venv_absent(doctor, pc, tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "_VENV_PYTHON", tmp_path / "no-such-venv" / "python")
    assert doctor._resolve_local_cognee_version() == "Not installed"


# ── embedding ──────────────────────────────────────────────────────────────


def test_embedding_default_when_unset(doctor, monkeypatch):
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    assert doctor._resolve_embedding() == ("Default", "Default")


def test_embedding_from_env(doctor, monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "openai/text-embedding-3-large")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "3072")
    assert doctor._resolve_embedding() == ("openai/text-embedding-3-large", "3072")


# ── circuit breaker ────────────────────────────────────────────────────────


def test_breaker_closed(doctor):
    assert doctor._resolve_circuit_breaker() == "Closed"


def test_breaker_open(doctor, suite, isolated_modules):
    # Per-server schema (SDK-356): entries are keyed by base_url; the no-URL
    # doctor view reports the worst open entry across all servers.
    client = isolated_modules(suite, "_cognee_client")
    state = client._state_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {"servers": {"http://x": {"cooldown_until": time.time() + 60, "reason": "unreachable"}}}
        ),
        encoding="utf-8",
    )
    assert doctor._resolve_circuit_breaker().startswith("Open")
