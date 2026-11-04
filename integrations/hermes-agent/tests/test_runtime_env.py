"""The private venv local mode starts its cognee server from (runtime_env).

Every subprocess is faked: these check which commands run, in what order, and
that the ready marker is written only once the pinned version is really there.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cognee_integration_hermes import runtime_env  # noqa: E402
from cognee_integration_hermes import server_bootstrap as sb  # noqa: E402
from cognee_integration_hermes.config import LOCAL_COGNEE_REQUIREMENT  # noqa: E402

PINNED = LOCAL_COGNEE_REQUIREMENT.split("==", 1)[1]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    monkeypatch.setattr(runtime_env, "RUNTIME_DIR", root)
    monkeypatch.setattr(runtime_env, "VENV_DIR", root / "venv")
    monkeypatch.setattr(runtime_env, "READY_MARKER", root / "ready.json")
    return root


def _python(root):
    return runtime_env.venv_python(root / "venv")


def _mark_ready(root, requirement=LOCAL_COGNEE_REQUIREMENT):
    python = _python(root)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    (root / "ready.json").write_text(json.dumps({"requirement": requirement}))


class _FakeRun:
    """Records install commands; makes `venv` create the interpreter file."""

    def __init__(self, root, version=PINNED, fail_on=None):
        self.root, self.version, self.fail_on = root, version, fail_on
        self.commands = []
        self.envs = []

    def __call__(self, command, **kwargs):
        command = [str(c) for c in command]
        if command[-2:] == ["-c", "import importlib.metadata as m; print(m.version('cognee'))"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{self.version}\n", stderr="")
        self.commands.append(command)
        self.envs.append(kwargs.get("env"))
        if self.fail_on and self.fail_on in command:
            raise subprocess.CalledProcessError(2, command)
        if "venv" in command:
            _python(self.root).parent.mkdir(parents=True, exist_ok=True)
            _python(self.root).touch()
        return subprocess.CompletedProcess(command, 0)


def test_not_ready_without_a_marker(runtime):
    assert runtime_env.runtime_python() is None


def test_a_marker_for_another_version_is_not_ready(runtime):
    _mark_ready(runtime, requirement="cognee==0.0.1")
    assert runtime_env.runtime_python() is None


def test_ready_with_a_matching_marker(runtime):
    _mark_ready(runtime)
    assert runtime_env.runtime_python() == str(_python(runtime))


def test_server_python_prefers_the_runtime(runtime):
    _mark_ready(runtime)
    with mock.patch.object(runtime_env, "has_cognee", return_value=True):
        assert runtime_env.server_python() == str(_python(runtime))


def test_server_python_falls_back_to_a_hand_installed_cognee(runtime):
    with mock.patch.object(runtime_env, "has_cognee", return_value=True):
        assert runtime_env.server_python() == sys.executable
    with mock.patch.object(runtime_env, "has_cognee", return_value=False):
        assert runtime_env.server_python() is None


def test_install_with_uv_never_downloads_an_interpreter(runtime):
    run = _FakeRun(runtime)
    with (
        mock.patch.object(runtime_env, "_find_uv", return_value="/bin/uv"),
        mock.patch.object(runtime_env.subprocess, "run", side_effect=run),
    ):
        python = runtime_env.install_runtime(output=lambda _: None)
    assert python == str(_python(runtime))
    assert run.commands == [
        ["/bin/uv", "venv", str(runtime / "venv"), "--python", sys.executable],
        ["/bin/uv", "pip", "install", "--python", str(_python(runtime)), LOCAL_COGNEE_REQUIREMENT],
    ]
    assert all(env["UV_PYTHON_DOWNLOADS"] == "never" for env in run.envs)
    assert json.loads((runtime / "ready.json").read_text())["requirement"] == (
        LOCAL_COGNEE_REQUIREMENT
    )


def test_install_without_uv_uses_venv_and_pip(runtime):
    run = _FakeRun(runtime)
    with (
        mock.patch.object(runtime_env, "_find_uv", return_value=None),
        mock.patch.object(runtime_env.subprocess, "run", side_effect=run),
    ):
        runtime_env.install_runtime(output=lambda _: None)
    assert run.commands == [
        [sys.executable, "-m", "venv", str(runtime / "venv")],
        [str(_python(runtime)), "-m", "pip", "install", LOCAL_COGNEE_REQUIREMENT],
    ]
    assert runtime_env.runtime_python() == str(_python(runtime))


def test_a_ready_runtime_is_not_reinstalled(runtime):
    _mark_ready(runtime)
    with mock.patch.object(runtime_env.subprocess, "run") as run:
        runtime_env.install_runtime(output=lambda _: None)
    run.assert_not_called()


def test_an_older_runtime_is_upgraded_in_place(runtime):
    # A plugin update moves the pin: the existing venv is reused, cognee reinstalled.
    _mark_ready(runtime, requirement="cognee==0.0.1")
    run = _FakeRun(runtime)
    with (
        mock.patch.object(runtime_env, "_find_uv", return_value="/bin/uv"),
        mock.patch.object(runtime_env.subprocess, "run", side_effect=run),
    ):
        runtime_env.install_runtime(output=lambda _: None)
    assert [c[1] for c in run.commands] == ["pip"]
    assert runtime_env.runtime_python() == str(_python(runtime))


def test_a_failed_step_leaves_no_marker(runtime):
    run = _FakeRun(runtime, fail_on="install")
    with (
        mock.patch.object(runtime_env, "_find_uv", return_value="/bin/uv"),
        mock.patch.object(runtime_env.subprocess, "run", side_effect=run),
    ):
        with pytest.raises(runtime_env.RuntimeInstallError, match="Installing cognee with uv"):
            runtime_env.install_runtime(output=lambda _: None)
    assert not (runtime / "ready.json").exists()


def test_a_wrong_installed_version_leaves_no_marker(runtime):
    run = _FakeRun(runtime, version="0.0.1")
    with (
        mock.patch.object(runtime_env, "_find_uv", return_value="/bin/uv"),
        mock.patch.object(runtime_env.subprocess, "run", side_effect=run),
    ):
        with pytest.raises(runtime_env.RuntimeInstallError, match="expected"):
            runtime_env.install_runtime(output=lambda _: None)
    assert runtime_env.runtime_python() is None


def test_remove_runtime(runtime):
    assert runtime_env.remove_runtime() is False
    _mark_ready(runtime)
    assert runtime_env.remove_runtime() is True
    assert runtime_env.runtime_python() is None
    assert not (runtime / "venv").exists()


def test_the_server_is_spawned_with_the_runtime_interpreter():
    with (
        mock.patch.object(sb, "health_ok", side_effect=[False, True]),
        mock.patch.object(sb, "server_python", return_value="/runtime/bin/python"),
        mock.patch.object(sb.subprocess, "Popen") as popen,
        mock.patch.object(sb.time, "sleep"),
    ):
        sb.ensure_local_server(8011, log_path="/dev/null")
    argv = popen.call_args.args[0]
    assert argv[:4] == ["/runtime/bin/python", "-m", "uvicorn", "cognee.api.client:app"]
