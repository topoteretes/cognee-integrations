"""What `cognee-doctor` reports, for everything it resolves locally.

`doctor.py` is the first thing a user runs when memory "isn't working", so each
line has to be right about where the plugin is actually pointed: Local (no URL)
vs Local Managed (loopback URL) vs Cloud, which endpoint won, whether the API key
came from the environment or the cached file, the embedding settings, and the
circuit-breaker state.

The endpoint-resolution precedence (env > localhost default) is asserted for
both suites here. There is no config-file layer any more — a `config.json` that
only SessionStart honoured is how a stale URL split the plugin across two servers
(SDK-466) — and codex's `doctor.py` keeps its own `_API_KEY_CACHE` constant,
which the API-key fixture accounts for.

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


# ── endpoint precedence: env > localhost default ───────────────────────────


def test_endpoint_defaults_to_localhost(pc):
    """The harness scrubs COGNEE_*, so nothing configured means the local default."""
    assert pc._local_api_url_with_source() == ("http://localhost:8011", "default_local")


def test_endpoint_env_vars_precedence(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", "http://from-base-env:8013")
    assert pc._local_api_url_with_source() == ("http://from-base-env:8013", "env_service_url")

    monkeypatch.setenv("COGNEE_LOCAL_API_URL", "http://from-local-env:8014")
    assert pc._local_api_url_with_source() == ("http://from-local-env:8014", "env_local_api_url")


def test_legacy_config_json_is_ignored(pc, suite, isolated_modules, temp_home):
    """A leftover config.json must not steer the endpoint — that file is exactly
    how a stale cloud URL used to disable memory (SDK-466). Both historical
    locations are planted: the shared root (codex) and the suite's state dir."""
    from utils.suites import plugin_root, state_dir

    config = isolated_modules(suite, "config")
    for path in (
        plugin_root(temp_home) / "config.json",
        state_dir(suite, temp_home) / "config.json",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"base_url": "http://stale-managed:8012"}), encoding="utf-8")
    assert pc._local_api_url_with_source() == ("http://localhost:8011", "default_local")
    assert config.load_config().get("base_url") == ""


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


def test_cached_api_key_remains_scoped_to_its_endpoint(pc, tmp_path, monkeypatch):
    """A key cached for another server must never be sent to this one."""
    configured = "http://managed-cognee.internal:8012"
    monkeypatch.setenv("COGNEE_BASE_URL", configured)
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
