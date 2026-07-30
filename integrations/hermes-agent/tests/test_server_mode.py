"""Tests for local-server mode: bootstrap helper, init mode routing, config.

Mode selection is a Hermes concern and stays in the provider: which transport
gets built, and whether a local server is spawned first. The transport itself is
faked, so these exercise pure routing logic and need neither cognee nor a network.
How a transport turns protocol calls into wire format lives in
``test_sdk_backend.py``.

Runs under pytest or standalone (``python3 tests/test_server_mode.py``).
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _char_helpers import FakeBackend  # noqa: E402
from cognee_integration_hermes import config as config_mod  # noqa: E402
from cognee_integration_hermes import provider as provider_mod  # noqa: E402
from cognee_integration_hermes import server_bootstrap as sb  # noqa: E402
from cognee_integration_hermes.backend import SdkBackend  # noqa: E402
from cognee_integration_hermes.http_backend import HttpBackend  # noqa: E402


class _FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestHealthOk(unittest.TestCase):
    def test_2xx_is_healthy(self):
        with mock.patch.object(sb.urllib.request, "urlopen", return_value=_FakeResp(200)):
            self.assertTrue(sb.health_ok("http://127.0.0.1:8000"))

    def test_5xx_is_not_healthy(self):
        with mock.patch.object(sb.urllib.request, "urlopen", return_value=_FakeResp(503)):
            self.assertFalse(sb.health_ok("http://127.0.0.1:8000"))

    def test_connection_error_is_not_healthy(self):
        with mock.patch.object(sb.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertFalse(sb.health_ok("http://127.0.0.1:8000"))


class TestEnsureLocalServer(unittest.TestCase):
    def test_already_healthy_does_not_spawn(self):
        with (
            mock.patch.object(sb, "health_ok", return_value=True),
            mock.patch.object(sb, "_spawn") as spawn,
        ):
            url = sb.ensure_local_server(8000)
        self.assertEqual(url, "http://127.0.0.1:8000")
        spawn.assert_not_called()

    def test_spawns_then_polls_until_healthy(self):
        # First probe down (-> spawn), second probe up (-> return).
        with (
            mock.patch.object(sb, "health_ok", side_effect=[False, True]),
            mock.patch.object(sb, "_spawn") as spawn,
            mock.patch.object(sb.time, "sleep"),
        ):
            url = sb.ensure_local_server(8123)
        self.assertEqual(url, "http://127.0.0.1:8123")
        spawn.assert_called_once()

    def test_raises_when_never_healthy(self):
        with (
            mock.patch.object(sb, "health_ok", return_value=False),
            mock.patch.object(sb, "_spawn"),
            mock.patch.object(sb.time, "sleep"),
        ):
            with self.assertRaises(RuntimeError):
                sb.ensure_local_server(8000, boot_timeout=0.01)


class TestSpawnEnvironment(unittest.TestCase):
    """The spawned server's environment — where the session cache lives or dies."""

    def _spawn_env(self, **env_overrides):
        captured = {}

        def fake_popen(args, env=None, **kwargs):
            captured.update(env or {})
            return mock.MagicMock()

        with (
            mock.patch.dict("os.environ", env_overrides, clear=False),
            mock.patch.object(sb.subprocess, "Popen", side_effect=fake_popen),
        ):
            sb._spawn(9999, "", "", "/dev/null")
        return captured

    def test_caching_is_enabled_for_the_session_tier(self):
        # Live-diagnosed: without CACHING, cognee's session manager reports
        # is_available=False and a session write is dropped while the API still
        # answers status="session_stored" — so turns silently never reach the graph.
        self.assertEqual(self._spawn_env()["CACHING"], "true")

    def test_auto_feedback_is_enabled(self):
        self.assertEqual(self._spawn_env()["AUTO_FEEDBACK"], "true")

    def test_agent_mode_is_enabled_so_the_server_can_retire(self):
        self.assertEqual(self._spawn_env()["COGNEE_AGENT_MODE"], "true")

    def test_the_env_matches_the_other_plugins_bootstraps(self):
        # The server must behave identically no matter which cognee plugin booted
        # it — these mirror claude-code's apply_cognee_env.
        env = self._spawn_env()
        self.assertEqual(env["CACHE_ROOT_DIRECTORY"], str(config_mod.SHARED_COGNEE_HOME / "cache"))
        self.assertEqual(env["LLM_INSTRUCTOR_MODE"], "json_schema_mode")
        self.assertEqual(env["COGNEE_IMPROVE_SUBMIT_TIMEOUT"], "420")

    def test_an_explicit_user_value_wins(self):
        self.assertEqual(self._spawn_env(CACHING="false")["CACHING"], "false")

    def test_data_roots_are_passed_through_when_given(self):
        captured = {}

        def fake_popen(args, env=None, **kwargs):
            captured.update(env or {})
            return mock.MagicMock()

        with mock.patch.object(sb.subprocess, "Popen", side_effect=fake_popen):
            sb._spawn(9999, "/tmp/data", "/tmp/system", "/dev/null")
        self.assertEqual(captured["DATA_ROOT_DIRECTORY"], "/tmp/data")
        self.assertEqual(captured["SYSTEM_ROOT_DIRECTORY"], "/tmp/system")

    def test_roots_are_omitted_when_empty(self):
        env = self._spawn_env()
        # Not asserting absence outright: the ambient environment may legitimately
        # carry them. What matters is we do not invent an empty value.
        self.assertNotEqual(env.get("DATA_ROOT_DIRECTORY", "unset"), "")


class _Recorder(dict):
    """Reads the mode-routing decisions off a fake backend.

    Mode selection stays in the provider — which transport gets built, and
    whether a local server is spawned first, is a Hermes concern. What the
    provider then *asks* the transport to do is recorded here.
    """

    def __init__(self, backend):
        super().__init__(served=None, identity_called=False, roots_called=False)
        self._backend = backend

    def _refresh(self):
        connects = self._backend.kwargs_for("connect")
        if connects:
            self["served"] = (connects[0]["url"], connects[0]["api_key"])
        self["identity_called"] = bool(self._backend.kwargs_for("resolve_identity"))
        self["roots_called"] = bool(self._backend.kwargs_for("configure_local_roots"))

    def __getitem__(self, key):
        self._refresh()
        return super().__getitem__(key)


def _make_provider():
    """A provider on a fake transport, plus a recorder of what it was asked to do."""
    backend = FakeBackend()
    p = provider_mod.CogneeMemoryProvider(backend=backend)
    return p, _Recorder(backend)


_NO_URL = {"COGNEE_BASE_URL": "", "COGNEE_SERVICE_URL": ""}


class TestInitializeModes(unittest.TestCase):
    def test_remote_mode_serves_service_url_and_skips_local_identity(self):
        env = {**_NO_URL, "COGNEE_BASE_URL": "https://cloud.example/api", "COGNEE_API_KEY": "k"}
        p, rec = _make_provider()
        with mock.patch.dict("os.environ", env, clear=False):
            p.initialize("sid")
        self.assertTrue(p._remote_mode)
        self.assertEqual(rec["served"], ("https://cloud.example/api", "k"))
        self.assertFalse(rec["identity_called"])
        self.assertFalse(rec["roots_called"])

    def test_embedded_mode_configures_shared_roots_and_resolves_identity(self):
        env = {
            **_NO_URL,
            "COGNEE_EMBEDDED": "true",
            "COGNEE_DATA_ROOT": "",
            "COGNEE_SYSTEM_ROOT": "",
        }
        p, rec = _make_provider()
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict("os.environ", env, clear=False):
                p.initialize("sid", hermes_home=home)
            self.assertFalse(p._remote_mode)
            self.assertIsNone(rec["served"])
            self.assertTrue(rec["roots_called"])
            self.assertTrue(rec["identity_called"])
            roots = p._backend.only_call("configure_local_roots")
        # The store is the shared ~/.cognee — the same roots the other cognee
        # plugins pin — not scoped to the Hermes profile.
        self.assertEqual(roots["data_root"], str(config_mod.SHARED_COGNEE_HOME / "data"))
        self.assertEqual(roots["system_root"], str(config_mod.SHARED_COGNEE_HOME / "system"))

    def test_embedded_mode_without_hermes_home_still_configures_roots(self):
        env = {
            **_NO_URL,
            "COGNEE_EMBEDDED": "true",
            "COGNEE_DATA_ROOT": "",
            "COGNEE_SYSTEM_ROOT": "",
        }
        p, rec = _make_provider()
        with mock.patch.dict("os.environ", env, clear=False):
            p.initialize("sid")
        self.assertTrue(rec["roots_called"])

    def test_default_mode_ensures_local_server_and_serves_localhost(self):
        env = {**_NO_URL, "COGNEE_EMBEDDED": ""}
        p, rec = _make_provider()
        with (
            mock.patch.dict("os.environ", env, clear=False),
            mock.patch.object(
                provider_mod, "ensure_local_server", return_value="http://127.0.0.1:8000"
            ) as ensure,
        ):
            p.initialize("sid")
        ensure.assert_called_once()
        # The port actually handed to the bootstrap, not just the mocked return:
        # 8011 keeps us off cognee's own default of 8000, so we never attach to a
        # server the user is running themselves.
        self.assertEqual(ensure.call_args.args[0], 8011)
        self.assertTrue(p._remote_mode)
        self.assertEqual(rec["served"], ("http://127.0.0.1:8000", ""))
        self.assertFalse(rec["identity_called"])

    def test_local_server_failure_raises_not_silently_embedded(self):
        # Falling back to embedded would reintroduce the DB-lock risk this PR
        # removes, so a server that won't start is a hard error.
        env = {**_NO_URL, "COGNEE_EMBEDDED": ""}
        p, rec = _make_provider()
        with (
            mock.patch.dict("os.environ", env, clear=False),
            mock.patch.object(
                provider_mod, "ensure_local_server", side_effect=RuntimeError("no server")
            ),
        ):
            with self.assertRaises(RuntimeError):
                p.initialize("sid")
        self.assertFalse(rec["roots_called"])  # did NOT silently drop to embedded

    def test_remote_failure_raises_not_silently_local(self):
        # An explicit remote URL that fails must surface, not silently diverge to a
        # local graph (data divergence / masked config error).
        env = {**_NO_URL, "COGNEE_BASE_URL": "https://cloud.example/api"}
        p, rec = _make_provider()
        p._backend.errors["connect"] = RuntimeError("unreachable")
        with mock.patch.dict("os.environ", env, clear=False):
            with self.assertRaises(RuntimeError):
                p.initialize("sid")
        self.assertFalse(rec["roots_called"])


class TestImproveBackgroundDecision(unittest.TestCase):
    """on_session_end backgrounds improve only when a server will finish the job."""

    def _run_session_end(self, *, remote_mode, env_override=None):
        p, _ = _make_provider()
        p._initialized = True
        p._writes_enabled = True
        p._improve_on_end = True
        p._remote_mode = remote_mode
        p._config = {"improve_timeout": 300, "improve_background": env_override or ""}
        p._is_breaker_open = lambda: False
        p.on_session_end([])
        return p._backend.only_call("improve")["background"]

    def test_server_mode_backgrounds(self):
        self.assertTrue(self._run_session_end(remote_mode=True))

    def test_embedded_mode_runs_synchronously(self):
        self.assertFalse(self._run_session_end(remote_mode=False))

    def test_env_override_forces_background_in_embedded(self):
        self.assertTrue(self._run_session_end(remote_mode=False, env_override="true"))


class TestTransportSelection(unittest.TestCase):
    """Direct HTTP is the default; the SDK serves embedded mode and opt-out."""

    def _transport_for(self, env):
        # Real transport classes, so the type assertions mean something — but every
        # I/O-touching hook is stubbed: SdkBackend's cognee imports would cost
        # seconds, and connect() would reach for a socket.
        merged = {**_NO_URL, **env}
        provider = provider_mod.CogneeMemoryProvider()
        with (
            mock.patch.dict("os.environ", merged, clear=False),
            mock.patch.object(
                provider_mod, "ensure_local_server", return_value="http://127.0.0.1:8000"
            ),
            mock.patch.object(SdkBackend, "configure_models"),
            mock.patch.object(SdkBackend, "configure_local_roots"),
            mock.patch.object(SdkBackend, "resolve_identity"),
            mock.patch.object(SdkBackend, "connect"),
            mock.patch.object(HttpBackend, "connect"),
        ):
            provider.initialize("sid")
        return type(provider._backend).__name__

    def test_default_is_the_http_transport(self):
        self.assertEqual(self._transport_for({}), "HttpBackend")

    def test_cloud_mode_also_uses_http(self):
        self.assertEqual(
            self._transport_for({"COGNEE_BASE_URL": "https://cloud.example"}), "HttpBackend"
        )

    def test_the_sdk_can_be_selected_explicitly(self):
        self.assertEqual(self._transport_for({"COGNEE_TRANSPORT": "sdk"}), "SdkBackend")

    def test_embedded_mode_uses_the_sdk(self):
        # Embedded means "no server at all", which only the in-process SDK can do.
        self.assertEqual(self._transport_for({"COGNEE_EMBEDDED": "true"}), "SdkBackend")

    def test_embedded_mode_wins_over_the_http_default(self):
        self.assertEqual(
            self._transport_for({"COGNEE_EMBEDDED": "true", "COGNEE_TRANSPORT": "http"}),
            "SdkBackend",
        )

    def test_an_injected_transport_always_wins(self):
        backend = FakeBackend()
        provider = provider_mod.CogneeMemoryProvider(backend=backend)
        env = {**_NO_URL, "COGNEE_EMBEDDED": "true", "COGNEE_TRANSPORT": "http"}
        with mock.patch.dict("os.environ", env, clear=False):
            provider.initialize("sid")
        self.assertIs(provider._backend, backend)

    def test_the_http_transport_caches_its_key_in_the_shared_state_dir(self):
        # One principal key per machine, shared with the other cognee plugins —
        # whichever plugin mints first, the rest (including Hermes) reuse it.
        provider = provider_mod.CogneeMemoryProvider()
        with tempfile.TemporaryDirectory() as home:
            with (
                mock.patch.dict("os.environ", _NO_URL, clear=False),
                mock.patch.object(
                    provider_mod, "ensure_local_server", return_value="http://127.0.0.1:8000"
                ),
                mock.patch.object(HttpBackend, "connect"),
            ):
                provider.initialize("sid", hermes_home=home)
            self.assertEqual(provider._backend._cache_dir, config_mod.SHARED_PLUGIN_STATE_DIR)


class TestConfigModes(unittest.TestCase):
    def test_base_url_preferred_over_service_url(self):
        env = {"COGNEE_BASE_URL": "https://canonical", "COGNEE_SERVICE_URL": "https://legacy"}
        with mock.patch.dict("os.environ", env, clear=False):
            cfg = config_mod.load_config()
        self.assertEqual(cfg["service_url"], "https://canonical")

    def test_service_url_used_when_base_url_absent(self):
        env = {**_NO_URL, "COGNEE_SERVICE_URL": "https://legacy"}
        with mock.patch.dict("os.environ", env, clear=False):
            cfg = config_mod.load_config()
        self.assertEqual(cfg["service_url"], "https://legacy")

    def test_embedded_and_port_defaults(self):
        env = {**_NO_URL, "COGNEE_EMBEDDED": "", "COGNEE_LOCAL_PORT": ""}
        with mock.patch.dict("os.environ", env, clear=False):
            cfg = config_mod.load_config()
        self.assertFalse(cfg["embedded"])
        self.assertEqual(cfg["local_port"], 8011)

    def test_local_port_clamped(self):
        env = {**_NO_URL, "COGNEE_LOCAL_PORT": "999999"}
        with mock.patch.dict("os.environ", env, clear=False):
            cfg = config_mod.load_config()
        self.assertEqual(cfg["local_port"], 65535)


if __name__ == "__main__":
    unittest.main(verbosity=2)
