"""cognee is not a dependency: remote mode works without it, local mode says what is missing.

Hermes resolves every plugin into one shared environment, and cognee's pins
conflict with Hermes extras there, which refused the install for everyone. So
the package declares no cognee at all, and the two paths that run cognee in this
interpreter (spawning a local server, embedded mode) check for it themselves.
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, our floor; pytest brings tomli there
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _char_helpers import FakeBackend  # noqa: E402
from cognee_integration_hermes import provider as provider_mod  # noqa: E402
from cognee_integration_hermes import server_bootstrap as sb  # noqa: E402
from cognee_integration_hermes.backend import CogneeNotInstalledError, SdkBackend  # noqa: E402
from cognee_integration_hermes.config import LOCAL_COGNEE_REQUIREMENT  # noqa: E402


def _requirements(text):
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def test_requirements_file_matches_the_constant():
    text = (ROOT / "local-mode-requirements.txt").read_text(encoding="utf-8")
    assert _requirements(text) == [LOCAL_COGNEE_REQUIREMENT]


def test_pyproject_declares_cognee_nowhere():
    # Hermes's lock resolves extras and dependency groups too, so cognee in any
    # of them brings the conflict back.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = list(project["project"].get("dependencies", []))
    for specs in project["project"].get("optional-dependencies", {}).values():
        declared += specs
    for specs in project.get("dependency-groups", {}).values():
        declared += [s for s in specs if isinstance(s, str)]
    assert not [s for s in declared if s.split("[")[0].split("=")[0].strip() == "cognee"]


def test_manifests_declare_no_python_dependencies():
    for path in (ROOT / "plugin.yaml", ROOT / "cognee_integration_hermes/_plugin_root/plugin.yaml"):
        text = path.read_text(encoding="utf-8")
        assert "pip_dependencies" not in text and "python_dependencies" not in text


@pytest.mark.parametrize(
    ("config", "cognee", "expected"),
    [
        ({"service_url": "https://tenant.example"}, False, True),
        ({"service_url": "https://tenant.example"}, True, True),
        ({"llm_api_key": "sk"}, False, False),
        ({"llm_api_key": "sk"}, True, True),
        # Ollama needs no key, and a gateway may not have loaded a new one yet;
        # the server bootstrap warns when it starts without a key.
        ({}, True, True),
        ({}, False, False),
    ],
)
def test_is_available(config, cognee, expected):
    p = provider_mod.CogneeMemoryProvider(backend=FakeBackend())
    with (
        mock.patch.object(provider_mod, "load_config", return_value=config),
        mock.patch.object(provider_mod.runtime_env, "local_ready", return_value=cognee),
    ):
        assert p.is_available() is expected


@pytest.mark.parametrize(
    ("config", "ready", "cognee", "expected"),
    [
        ({"service_url": "https://tenant.example"}, False, False, None),
        ({}, True, False, None),
        ({}, False, False, "hermes cognee install-runtime"),
        ({"embedded": "true"}, False, False, "Hermes's own Python environment"),
        ({"embedded": "true"}, True, True, None),
    ],
)
def test_unavailable_reason_names_the_fix(config, ready, cognee, expected):
    # Hermes never calls initialize() for an unavailable provider; this hint is
    # the only text of ours the user sees.
    p = provider_mod.CogneeMemoryProvider(backend=FakeBackend())
    with (
        mock.patch.object(provider_mod, "load_config", return_value=config),
        mock.patch.object(provider_mod.runtime_env, "local_ready", return_value=ready),
        mock.patch.object(provider_mod, "has_cognee", return_value=cognee),
    ):
        reason = p.unavailable_reason()
    if expected is None:
        assert reason == ""
    else:
        assert expected in reason


def test_a_running_server_is_used_without_cognee():
    with (
        mock.patch.object(sb, "health_ok", return_value=True),
        mock.patch.object(sb, "server_python", return_value=None),
        mock.patch.object(sb, "_spawn") as spawn,
    ):
        assert sb.ensure_local_server(8011) == "http://127.0.0.1:8011"
    spawn.assert_not_called()


def test_no_server_and_no_cognee_fails_without_spawning():
    with (
        mock.patch.object(sb, "health_ok", return_value=False),
        mock.patch.object(sb, "server_python", return_value=None),
        mock.patch.object(sb, "_spawn") as spawn,
    ):
        with pytest.raises(CogneeNotInstalledError, match="hermes cognee install-runtime"):
            sb.ensure_local_server(8011, boot_timeout=0)
    spawn.assert_not_called()


def test_local_mode_without_cognee_reports_the_fix_not_the_generic_error(monkeypatch):
    for key in ("COGNEE_BASE_URL", "COGNEE_SERVICE_URL", "COGNEE_EMBEDDED"):
        monkeypatch.setenv(key, "")
    p = provider_mod.CogneeMemoryProvider(backend=FakeBackend())
    with (
        mock.patch.object(sb, "health_ok", return_value=False),
        mock.patch.object(sb, "server_python", return_value=None),
    ):
        with pytest.raises(CogneeNotInstalledError, match="install-runtime"):
            p.initialize("sid")


def test_embedded_mode_without_cognee_says_so():
    backend = SdkBackend()
    with mock.patch("cognee_integration_hermes.backend.has_cognee", return_value=False):
        with pytest.raises(CogneeNotInstalledError, match="embedded mode"):
            backend.configure_local_roots(data_root="/tmp/d", system_root="/tmp/s")


def _run_setup(mode, current):
    answers = {"Mode": mode}
    saved = {}
    with (
        mock.patch.object(provider_mod, "load_config", return_value=current),
        mock.patch.object(
            provider_mod,
            "_prompt",
            side_effect=lambda label, default="": answers.get(label, default),
        ),
        mock.patch.object(provider_mod, "_prompt_secret", return_value=""),
        mock.patch.object(provider_mod, "_prompt_bool", side_effect=lambda label, default: default),
        mock.patch.object(provider_mod.runtime_env, "local_ready", return_value=True),
        mock.patch.object(provider_mod, "save_plugin_config"),
        mock.patch.object(
            provider_mod, "save_env_vars", side_effect=lambda values, home: saved.update(values)
        ),
    ):
        provider_mod.CogneeMemoryProvider(backend=FakeBackend()).post_setup("/tmp/hh", {})
    return saved


def test_switching_to_local_clears_the_cloud_url_and_key():
    cloud = {"service_url": "https://tenant.example", "api_key": "cloud-key"}
    saved = _run_setup("local", cloud)
    assert saved["COGNEE_BASE_URL"] == ""
    assert saved["COGNEE_SERVICE_URL"] == ""
    assert saved["COGNEE_API_KEY"] == ""


def test_staying_remote_keeps_the_key():
    cloud = {"service_url": "https://tenant.example", "api_key": "cloud-key"}
    saved = _run_setup("remote", cloud)
    assert "COGNEE_API_KEY" not in saved  # blank answer keeps the current key


def _default_mode(tmp_path, saved_config, runtime_ready):
    if saved_config is not None:
        (tmp_path / "cognee.json").write_text(json.dumps(saved_config))
    offered = {}

    def prompt(label, default=""):
        if label == "Mode":
            offered["mode"] = default
        return default

    with (
        mock.patch.object(provider_mod, "load_config", return_value={}),
        mock.patch.object(provider_mod, "config_path", return_value=tmp_path / "cognee.json"),
        mock.patch.object(provider_mod, "_prompt", side_effect=prompt),
        mock.patch.object(provider_mod, "_prompt_secret", return_value=""),
        mock.patch.object(provider_mod, "_prompt_bool", return_value=False),
        mock.patch.object(provider_mod.runtime_env, "local_ready", return_value=runtime_ready),
        mock.patch.object(provider_mod, "save_plugin_config"),
        mock.patch.object(provider_mod, "save_env_vars"),
    ):
        provider_mod.CogneeMemoryProvider(backend=FakeBackend()).post_setup(str(tmp_path), {})
    return offered["mode"]


@pytest.mark.parametrize(
    ("saved_config", "runtime_ready", "expected"),
    [
        (None, False, "remote"),  # first setup
        (None, True, "remote"),  # first setup, runtime already there
        ({"service_url": "https://tenant.example"}, True, "remote"),  # was remote
        ({"service_url": ""}, True, "remote"),  # was local: still offers remote
    ],
)
def test_setup_defaults_to_remote(tmp_path, saved_config, runtime_ready, expected):
    assert _default_mode(tmp_path, saved_config, runtime_ready) == expected


def test_a_local_install_is_told_its_current_mode(tmp_path, capsys):
    _default_mode(tmp_path, {"service_url": ""}, True)
    assert "Current mode: local" in capsys.readouterr().out
