"""Plugin identity (SDK-646): a dedicated ``hermes-agent`` cognee agent.

``COGNEE_PLUGIN_IDENTITY`` picks the policy. ``auto`` (default) and ``false``
keep today's behaviour — every call runs as the principal — while ``true``
provisions the plugin's own agent through the server's provision route, caches
this machine's key, and runs the session as that agent. The provision call
carries the plugin's label and connection type, so the server needs no
hardcoded plugin list, and every registration names the plugin in its metadata.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest
from cognee_integration_hermes import plugin_identity
from cognee_integration_hermes.http_backend import HttpBackend
from cognee_integration_hermes.plugin_identity import (
    CONNECTION_TYPE,
    PLUGIN_KEY,
    PLUGIN_LABEL,
    PROVISION_PATH,
    PluginIdentityError,
    identity_mode,
)
from test_http_backend import FakeOpener, _Response

_URL = "http://127.0.0.1:8011"
PRINCIPAL = "principal-key"
AGENT_KEY = "agentkey-1"
AGENT_ID = "agent-uuid-1"

REGISTER = "/api/v1/agents/register"
OPENAPI_ADDITIVE = {"paths": {PROVISION_PATH: {"post": {"x-cognee-additive-plugin-keys": True}}}}
OPENAPI_ROTATING = {"paths": {PROVISION_PATH: {"post": {}}}}
PROVISIONED = {
    "pluginKey": PLUGIN_KEY,
    "agentId": AGENT_ID,
    "apiKey": AGENT_KEY,
    "keyId": "k1",
    "created": True,
}


def _http_error(path, status, detail="nope"):
    return urllib.error.HTTPError(
        _URL + path, status, "err", {}, io.BytesIO(json.dumps({"detail": detail}).encode())
    )


class ScriptedOpener(FakeOpener):
    """FakeOpener whose per-path outcome may be a list consumed in order."""

    def __call__(self, request, timeout=None):
        path = self._key(request.selector if hasattr(request, "selector") else request.full_url)
        queue = self.responses.get(path)
        if isinstance(queue, list):
            # Consume in order; the last outcome answers every later call.
            outcome = queue.pop(0) if len(queue) > 1 else queue[0]
            self.requests.append(self._entry(request, timeout))
            if isinstance(outcome, Exception):
                raise outcome
            return _Response(outcome)
        return super().__call__(request, timeout=timeout)

    @staticmethod
    def _entry(request, timeout):
        return {
            "method": request.get_method(),
            "path": request.selector if hasattr(request, "selector") else request.full_url,
            "url": request.full_url,
            "headers": {k.lower(): v for k, v in request.header_items()},
            "body": request.data,
            "timeout": timeout,
        }

    def requests_for(self, path):
        return [r for r in self.requests if r["path"] == path]


@pytest.fixture
def cache(monkeypatch, tmp_path):
    path = tmp_path / "hermes-agent" / "agent_key.json"
    monkeypatch.setattr(plugin_identity, "AGENT_KEY_CACHE", path)
    return path


def _opener(**overrides):
    responses = {
        "/health": {"status": "ok"},
        "/openapi.json": OPENAPI_ADDITIVE,
        PROVISION_PATH: PROVISIONED,
        REGISTER: {"registered": True, "activeAgents": 1},
        "/api/v1/agents/unregister": {"activeAgents": 0},
    }
    responses.update(overrides)
    return ScriptedOpener(responses)


def _connect(opener, mode, api_key=PRINCIPAL):
    backend = HttpBackend(opener=opener)
    backend.connect(url=_URL, api_key=api_key, timeout=5, plugin_identity=mode)
    return backend


# -- policy ------------------------------------------------------------------


class TestMode:
    def test_default_is_auto(self, monkeypatch):
        monkeypatch.delenv("COGNEE_PLUGIN_IDENTITY", raising=False)
        assert identity_mode("") == "auto"
        assert identity_mode(None) == "auto"

    def test_env_is_read_when_nothing_is_passed(self, monkeypatch):
        monkeypatch.setenv("COGNEE_PLUGIN_IDENTITY", "true")
        assert identity_mode("") == "enabled"

    @pytest.mark.parametrize("value", ["true", "1", "yes", "ON", True])
    def test_boolean_spellings_enable(self, value):
        assert identity_mode(value) == "enabled"

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", False])
    def test_boolean_spellings_disable(self, value):
        assert identity_mode(value) == "disabled"

    def test_garbage_is_a_config_error(self):
        with pytest.raises(ValueError, match="auto, true, or false"):
            identity_mode("sometimes")

    def test_config_exposes_the_knob(self, monkeypatch):
        from cognee_integration_hermes.config import load_config

        monkeypatch.setenv("COGNEE_PLUGIN_IDENTITY", "true")
        assert load_config()["plugin_identity"] == "true"


# -- auto / false: the principal, as before ------------------------------------


class TestPrincipalModes:
    @pytest.mark.parametrize("mode", ["", "auto", "false"])
    def test_no_provisioning_and_registration_names_the_plugin(self, cache, mode, monkeypatch):
        # ``auto`` provisions only in service of shared agent memory (see
        # test_shared_memory.py); with it opted out there is nothing to provision for.
        monkeypatch.setenv("COGNEE_SHARED_AGENT_MEMORY", "false")
        opener = _opener()
        backend = _connect(opener, mode)
        assert backend.api_key == PRINCIPAL
        assert not backend.identity_active
        assert PROVISION_PATH not in opener.paths()
        assert "/openapi.json" not in opener.paths()
        body = opener.json_body(REGISTER)
        assert body["type"] == CONNECTION_TYPE
        assert body["metadata"] == {"plugin_key": PLUGIN_KEY}
        assert not cache.exists()

    def test_false_ignores_a_cached_identity(self, cache):
        plugin_identity.save_cached(
            {
                "base_url": _URL,
                "api_key": AGENT_KEY,
                "agent_id": AGENT_ID,
                "principal_fingerprint": plugin_identity.fingerprint(PRINCIPAL),
            },
            cache,
        )
        backend = _connect(_opener(), "false")
        assert backend.api_key == PRINCIPAL

    def test_registration_failure_still_does_not_fail_the_session(self, cache):
        backend = _connect(_opener(**{REGISTER: _http_error(REGISTER, 500)}), "auto")
        assert backend.api_key == PRINCIPAL
        assert backend.registered is False


# -- true: provision and run as the agent --------------------------------------


class TestExplicitIdentity:
    def test_fresh_install_provisions_describes_itself_and_runs_as_the_agent(self, cache):
        opener = _opener()
        backend = _connect(opener, "true")

        provision = opener.request_for(PROVISION_PATH)
        # Provisioning authenticates as the PRINCIPAL and describes the plugin.
        assert provision["headers"]["x-api-key"] == PRINCIPAL
        assert json.loads(provision["body"]) == {
            "label": PLUGIN_LABEL,
            "connection_type": CONNECTION_TYPE,
        }
        # ...and the session then runs as the agent.
        assert backend.api_key == AGENT_KEY
        assert backend.principal_api_key == PRINCIPAL
        assert backend.agent_id == AGENT_ID
        assert backend.identity_active
        assert opener.request_for(REGISTER)["headers"]["x-api-key"] == AGENT_KEY
        assert backend.connection_info()["api_key"] == AGENT_KEY

        record = json.loads(cache.read_text())
        assert record["api_key"] == AGENT_KEY
        assert record["agent_id"] == AGENT_ID
        assert record["key_id"] == "k1"
        assert record["plugin_key"] == PLUGIN_KEY
        assert record["principal_fingerprint"] == plugin_identity.fingerprint(PRINCIPAL)
        assert oct(cache.stat().st_mode & 0o777) == "0o600"

    def test_cached_identity_is_reused_without_provisioning(self, cache):
        _connect(_opener(), "true")
        opener = _opener()
        backend = _connect(opener, "true")
        assert backend.api_key == AGENT_KEY
        assert PROVISION_PATH not in opener.paths()
        assert "/openapi.json" not in opener.paths()

    def test_cache_is_scoped_to_the_server_url(self, cache):
        _connect(_opener(), "true")
        opener = _opener()
        backend = HttpBackend(opener=opener)
        backend.connect(
            url="http://127.0.0.1:9999", api_key=PRINCIPAL, timeout=5, plugin_identity="true"
        )
        assert PROVISION_PATH in opener.paths()

    def test_identity_of_another_principal_is_refused(self, cache):
        _connect(_opener(), "true")
        with pytest.raises(PluginIdentityError, match="another principal"):
            _connect(_opener(), "true", api_key="someone-elses-key")

    def test_server_that_rotates_keys_is_never_provisioned_against(self, cache):
        opener = _opener(**{"/openapi.json": OPENAPI_ROTATING})
        with pytest.raises(PluginIdentityError, match="adds plugin keys per machine"):
            _connect(opener, "true")
        assert PROVISION_PATH not in opener.paths()
        assert not cache.exists()

    def test_unreadable_capability_is_not_permission_to_provision(self, cache):
        opener = _opener(**{"/openapi.json": _http_error("/openapi.json", 500)})
        with pytest.raises(PluginIdentityError):
            _connect(opener, "true")
        assert PROVISION_PATH not in opener.paths()

    def test_server_without_the_plugin_route_is_unsupported(self, cache):
        opener = _opener(**{PROVISION_PATH: _http_error(PROVISION_PATH, 404)})
        with pytest.raises(PluginIdentityError, match="unsupported"):
            _connect(opener, "true")

    def test_full_agent_names_the_keys_command(self, cache):
        opener = _opener(**{PROVISION_PATH: _http_error(PROVISION_PATH, 409)})
        with pytest.raises(PluginIdentityError, match="hermes cognee keys"):
            _connect(opener, "true")
        assert not cache.exists()

    def test_principal_handed_back_as_the_agent_key_is_rejected(self, cache):
        bad = {**PROVISIONED, "apiKey": PRINCIPAL}
        with pytest.raises(PluginIdentityError, match="agent_key_equals_principal"):
            _connect(_opener(**{PROVISION_PATH: bad}), "true")
        assert not cache.exists()

    def test_provision_without_an_agent_id_is_rejected(self, cache):
        bad = {"apiKey": AGENT_KEY, "created": True}
        with pytest.raises(PluginIdentityError, match="missing_agent_id"):
            _connect(_opener(**{PROVISION_PATH: bad}), "true")

    def test_snake_case_response_is_accepted(self, cache):
        snake = {"api_key": AGENT_KEY, "agent_id": AGENT_ID, "key_id": "k1", "created": False}
        backend = _connect(_opener(**{PROVISION_PATH: snake}), "true")
        assert backend.api_key == AGENT_KEY
        assert backend.agent_id == AGENT_ID

    def test_needs_a_principal_key(self, cache):
        # A local server with auth disabled mints nothing: identity has nothing
        # to provision with, and says so instead of running keyless.
        login = "/api/v1/auth/login"
        opener = _opener(**{login: _http_error(login, 400, "login_bad_credentials")})
        with pytest.raises(PluginIdentityError, match="principal API key"):
            _connect(opener, "true", api_key="")

    def test_rejected_key_is_blocked_and_replaced_in_the_same_launch(self, cache):
        plugin_identity.save_cached(
            {
                "base_url": _URL,
                "api_key": "agentkey-dead",
                "agent_id": AGENT_ID,
                "principal_fingerprint": plugin_identity.fingerprint(PRINCIPAL),
            },
            cache,
        )
        opener = _opener(**{REGISTER: [_http_error(REGISTER, 401), {"registered": True}]})
        backend = _connect(opener, "true")
        registers = opener.requests_for(REGISTER)
        assert [r["headers"]["x-api-key"] for r in registers] == ["agentkey-dead", AGENT_KEY]
        assert backend.api_key == AGENT_KEY
        assert backend.registered is True
        assert json.loads(cache.read_text())["api_key"] == AGENT_KEY
        assert not json.loads(cache.read_text()).get("blocked")

    def test_second_rejection_is_an_error_not_a_fallback(self, cache):
        plugin_identity.save_cached(
            {
                "base_url": _URL,
                "api_key": "agentkey-dead",
                "agent_id": AGENT_ID,
                "principal_fingerprint": plugin_identity.fingerprint(PRINCIPAL),
            },
            cache,
        )
        opener = _opener(**{REGISTER: [_http_error(REGISTER, 401), _http_error(REGISTER, 401)]})
        with pytest.raises(PluginIdentityError, match="could not be replaced"):
            _connect(opener, "true")
        assert json.loads(cache.read_text()).get("blocked") is True

    def test_forbidden_registration_keeps_the_key_and_raises(self, cache):
        opener = _opener(**{REGISTER: _http_error(REGISTER, 403)})
        with pytest.raises(PluginIdentityError, match="403"):
            _connect(opener, "true")
        record = json.loads(cache.read_text())
        assert record["api_key"] == AGENT_KEY
        assert not record.get("blocked")

    def test_blocked_cache_is_not_reused(self, cache):
        plugin_identity.save_cached(
            {
                "base_url": _URL,
                "api_key": "agentkey-dead",
                "agent_id": AGENT_ID,
                "principal_fingerprint": plugin_identity.fingerprint(PRINCIPAL),
                "blocked": True,
            },
            cache,
        )
        opener = _opener()
        backend = _connect(opener, "true")
        assert PROVISION_PATH in opener.paths()
        assert backend.api_key == AGENT_KEY

    def test_data_plane_calls_run_as_the_agent(self, cache):
        opener = _opener(**{"/api/v1/datasets": [{"id": "d", "name": "agent_sessions"}]})
        backend = _connect(opener, "true")
        backend.list_datasets(timeout=5)
        assert opener.request_for("/api/v1/datasets")["headers"]["x-api-key"] == AGENT_KEY


# -- the SDK transport cannot provision ------------------------------------------


def test_sdk_transport_warns_and_stays_on_the_principal(monkeypatch, caplog):
    from cognee_integration_hermes.backend import SdkBackend

    backend = SdkBackend()
    served = {}

    class _Bridge:
        def run(self, coro, timeout):
            coro.close()
            served["ok"] = True

    monkeypatch.setattr(backend, "_bridge", _Bridge())
    with caplog.at_level("WARNING"):
        backend.connect(url=_URL, api_key=PRINCIPAL, timeout=5, plugin_identity="true")
    assert served == {"ok": True}
    assert "needs the HTTP transport" in caplog.text


# -- keys command: Hermes's own agent is listed ---------------------------------


def test_keys_fallback_lists_hermes_own_agent():
    from cognee_integration_hermes.cli import FALLBACK_PLUGIN_KEYS

    assert PLUGIN_KEY in FALLBACK_PLUGIN_KEYS
