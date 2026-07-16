"""Tests for local-server mode: bootstrap helper, init mode routing, config.

Runs under pytest or standalone (``python3 tests/test_server_mode.py``). None of
these need cognee installed — the provider imports it lazily and we stub the
serve/identity coroutines, so the tests exercise pure routing logic.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cognee_integration_hermes import config as config_mod  # noqa: E402
from cognee_integration_hermes import provider as provider_mod  # noqa: E402
from cognee_integration_hermes import server_bootstrap as sb  # noqa: E402


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


def _make_provider():
    """A provider with cognee-touching coroutines stubbed; records what ran."""
    p = provider_mod.CogneeMemoryProvider()
    rec = {"served": None, "identity_called": False, "roots_called": False}

    async def fake_serve(url, key):
        rec["served"] = (url, key)

    async def fake_identity():
        rec["identity_called"] = True
        return "USER"

    p._do_serve = fake_serve
    p._ensure_identity = fake_identity
    p._configure_cognee_models = lambda: None
    p._configure_cognee_local_roots = lambda: rec.__setitem__("roots_called", True)
    return p, rec


_NO_URL = {"COGNEE_BASE_URL": "", "COGNEE_SERVICE_URL": ""}


class TestInitializeModes(unittest.TestCase):
    def test_remote_mode_serves_service_url_and_skips_local_identity(self):
        env = {**_NO_URL, "COGNEE_BASE_URL": "https://cloud.example/api", "COGNEE_API_KEY": "k"}
        p, rec = _make_provider()
        with mock.patch.dict("os.environ", env, clear=False):
            p.initialize("sid")
        self.assertTrue(p._remote_mode)
        self.assertEqual(rec["served"], ("https://cloud.example/api", "k"))
        self.assertIsNone(p._user)
        self.assertFalse(rec["identity_called"])
        self.assertFalse(rec["roots_called"])

    def test_embedded_mode_configures_roots_and_resolves_identity(self):
        env = {**_NO_URL, "COGNEE_EMBEDDED": "true"}
        p, rec = _make_provider()
        with mock.patch.dict("os.environ", env, clear=False):
            p.initialize("sid")
        self.assertFalse(p._remote_mode)
        self.assertIsNone(rec["served"])
        self.assertTrue(rec["roots_called"])
        self.assertTrue(rec["identity_called"])
        self.assertEqual(p._user, "USER")

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
        self.assertTrue(p._remote_mode)
        self.assertEqual(rec["served"], ("http://127.0.0.1:8000", ""))
        self.assertIsNone(p._user)
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

        async def boom(url, key):
            raise RuntimeError("unreachable")

        p._do_serve = boom
        with mock.patch.dict("os.environ", env, clear=False):
            with self.assertRaises(RuntimeError):
                p.initialize("sid")
        self.assertFalse(rec["roots_called"])


class TestUserKwarg(unittest.TestCase):
    def test_omitted_in_remote_mode_even_with_user_set(self):
        p, _ = _make_provider()
        p._remote_mode = True
        p._user = "USER"
        kwargs = {}
        p._add_user_kwarg(kwargs)
        self.assertNotIn("user", kwargs)  # omitted, not user=None

    def test_included_in_embedded_mode(self):
        p, _ = _make_provider()
        p._remote_mode = False
        p._user = "USER"
        kwargs = {}
        p._add_user_kwarg(kwargs)
        self.assertEqual(kwargs["user"], "USER")

    def test_omitted_when_user_is_none(self):
        p, _ = _make_provider()
        p._remote_mode = False
        p._user = None
        kwargs = {}
        p._add_user_kwarg(kwargs)
        self.assertNotIn("user", kwargs)


class TestImproveBackgroundDecision(unittest.TestCase):
    """on_session_end backgrounds improve only when a server will finish the job."""

    def _run_session_end(self, *, remote_mode, env_override=None):
        p, _ = _make_provider()
        p._initialized = True
        p._writes_enabled = True
        p._improve_on_end = True
        p._remote_mode = remote_mode
        p._config = {"improve_timeout": 300, "improve_background": env_override or ""}
        captured = {}

        async def fake_improve(run_in_background=False):
            captured["bg"] = run_in_background

        p._do_improve = fake_improve
        p._is_breaker_open = lambda: False
        p.on_session_end([])
        return captured.get("bg")

    def test_server_mode_backgrounds(self):
        self.assertTrue(self._run_session_end(remote_mode=True))

    def test_embedded_mode_runs_synchronously(self):
        self.assertFalse(self._run_session_end(remote_mode=False))

    def test_env_override_forces_background_in_embedded(self):
        self.assertTrue(self._run_session_end(remote_mode=False, env_override="true"))


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
        self.assertEqual(cfg["local_port"], 8000)

    def test_local_port_clamped(self):
        env = {**_NO_URL, "COGNEE_LOCAL_PORT": "999999"}
        with mock.patch.dict("os.environ", env, clear=False):
            cfg = config_mod.load_config()
        self.assertEqual(cfg["local_port"], 65535)


if __name__ == "__main__":
    unittest.main(verbosity=2)
