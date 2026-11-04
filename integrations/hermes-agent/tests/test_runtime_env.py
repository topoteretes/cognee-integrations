"""The private venv local mode starts its cognee server from (runtime_env).

Every subprocess is faked: these check which commands run, in what order, that
a new pin is built in a venv of its own, and that the ready marker moves to it
only once the pinned version is really there.
"""

import json
import os
import subprocess
import sys
import time
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
# conftest swaps it for a no-op so no test launches a real build; these tests
# fake Popen instead.
_REAL_START_BACKGROUND_UPGRADE = runtime_env.start_background_upgrade


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    monkeypatch.setattr(runtime_env, "RUNTIME_DIR", root)
    monkeypatch.setattr(runtime_env, "READY_MARKER", root / "ready.json")
    monkeypatch.setattr(runtime_env, "FAILURE_MARKER", root / "last-failure.json")
    monkeypatch.setattr(runtime_env, "UPGRADE_LOG", tmp_path / "runtime-upgrade.log")
    return root


def _new(root):
    """The interpreter of the current pin's venv."""
    return runtime_env.venv_python(root / f"venv-{PINNED}")


def _legacy(root):
    """The interpreter of the venv plugins before 1.5.0 built."""
    return runtime_env.venv_python(root / "venv")


def _make_venv(python):
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()


def _mark_ready(root, requirement=LOCAL_COGNEE_REQUIREMENT, python=None):
    """A marker as an older plugin wrote it (no python) unless one is given."""
    _make_venv(python or _legacy(root))
    marker = {"requirement": requirement}
    if python is not None:
        marker["python"] = str(python)
    (root / "ready.json").write_text(json.dumps(marker))


def _mark_outdated(root):
    """The 1.6.5-era layout: runtime/venv plus its marker, one pin behind."""
    _mark_ready(root, requirement="cognee==0.0.1", python=_legacy(root))
    (_legacy(root).parent.parent / "site.txt").write_text("old cognee")


class _FakeRun:
    """Records install commands; makes `venv` create the interpreter file."""

    def __init__(self, root, version=PINNED, fail_on=None, missing=()):
        self.root, self.version, self.fail_on, self.missing = root, version, fail_on, missing
        self.commands = []
        self.envs = []

    def __call__(self, command, **kwargs):
        command = [str(c) for c in command]
        if command[-2:] == ["-c", runtime_env._VERIFY_PROBE]:
            found = {"cognee": self.version, "missing": list(self.missing)}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(found), stderr="")
        self.commands.append(command)
        self.envs.append(kwargs.get("env"))
        if self.fail_on and self.fail_on in command:
            raise subprocess.CalledProcessError(2, command)
        if "venv" in command:
            _make_venv(runtime_env.venv_python(Path(command[command.index("venv") + 1])))
        return subprocess.CompletedProcess(command, 0)


def _install(root, uv="/bin/uv", **run_kwargs):
    run = _FakeRun(root, **run_kwargs)
    with (
        mock.patch.object(runtime_env, "_find_uv", return_value=uv),
        mock.patch.object(runtime_env.subprocess, "run", side_effect=run),
    ):
        python = runtime_env.install_runtime(output=lambda _: None)
    return python, run


# -- what counts as installed -------------------------------------------------


def test_not_ready_without_a_marker(runtime):
    assert runtime_env.runtime_python() is None


def test_a_marker_for_another_version_is_not_ready(runtime):
    _mark_ready(runtime, requirement="cognee==0.0.1")
    assert runtime_env.runtime_python() is None


def test_an_outdated_runtime_still_counts_as_installed(runtime):
    # An upgrade from a plugin with an older pin must not turn the provider off.
    _mark_ready(runtime, requirement="cognee==0.0.1")
    with mock.patch.object(runtime_env, "has_cognee", return_value=False):
        assert runtime_env.outdated_requirement() == "cognee==0.0.1"
        assert runtime_env.server_python() == str(_legacy(runtime))
        assert runtime_env.local_ready() is True


def test_an_outdated_marker_without_its_venv_is_not_installed(runtime):
    _mark_ready(runtime, requirement="cognee==0.0.1")
    _legacy(runtime).unlink()
    with mock.patch.object(runtime_env, "has_cognee", return_value=False):
        assert runtime_env.outdated_requirement() is None
        assert runtime_env.local_ready() is False


def test_a_current_runtime_is_not_outdated(runtime):
    _mark_ready(runtime)
    assert runtime_env.outdated_requirement() is None


def test_a_marker_without_a_python_means_the_legacy_venv(runtime):
    # 1.4.x wrote markers both with and without "python"; both name runtime/venv.
    _mark_ready(runtime)
    assert runtime_env.runtime_python() == str(_legacy(runtime))
    assert runtime_env.active_venv_dir() == runtime / "venv"


def test_the_marker_python_decides_the_active_runtime(runtime):
    _mark_ready(runtime, python=_new(runtime))
    _make_venv(_legacy(runtime))
    assert runtime_env.runtime_python() == str(_new(runtime))
    assert runtime_env.leftover_runtime_dirs() == [runtime / "venv"]


def test_server_python_prefers_the_runtime(runtime):
    _mark_ready(runtime, python=_new(runtime))
    with mock.patch.object(runtime_env, "has_cognee", return_value=True):
        assert runtime_env.server_python() == str(_new(runtime))


def test_server_python_falls_back_to_a_hand_installed_cognee(runtime):
    with mock.patch.object(runtime_env, "has_cognee", return_value=True):
        assert runtime_env.server_python() == sys.executable
    with mock.patch.object(runtime_env, "has_cognee", return_value=False):
        assert runtime_env.server_python() is None


# -- building ---------------------------------------------------------------


def test_install_with_uv_never_downloads_an_interpreter(runtime):
    python, run = _install(runtime)
    target = runtime / f"venv-{PINNED}"
    assert python == str(_new(runtime))
    assert run.commands == [
        ["/bin/uv", "venv", str(target), "--python", sys.executable],
        ["/bin/uv", "pip", "install", "--python", str(_new(runtime)), LOCAL_COGNEE_REQUIREMENT],
    ]
    assert all(env["UV_PYTHON_DOWNLOADS"] == "never" for env in run.envs)
    assert json.loads((runtime / "ready.json").read_text()) == {
        "requirement": LOCAL_COGNEE_REQUIREMENT,
        "python": str(_new(runtime)),
    }


def test_install_without_uv_uses_venv_and_pip(runtime):
    _, run = _install(runtime, uv=None)
    assert run.commands == [
        [sys.executable, "-m", "venv", str(runtime / f"venv-{PINNED}")],
        [str(_new(runtime)), "-m", "pip", "install", LOCAL_COGNEE_REQUIREMENT],
    ]
    assert runtime_env.runtime_python() == str(_new(runtime))


def test_a_ready_runtime_is_not_reinstalled(runtime):
    _mark_ready(runtime)
    with mock.patch.object(runtime_env.subprocess, "run") as run:
        runtime_env.install_runtime(output=lambda _: None)
    run.assert_not_called()


def test_an_upgrade_is_built_in_a_new_venv_and_leaves_the_old_one_alone(runtime):
    _mark_outdated(runtime)
    python, run = _install(runtime)
    assert python == str(_new(runtime))
    # Nothing ran against the old venv, and it is still all there.
    assert not any(str(runtime / "venv") + os.sep in " ".join(c) for c in run.commands)
    assert (runtime / "venv" / "site.txt").read_text() == "old cognee"
    assert _legacy(runtime).exists()
    assert runtime_env.runtime_python() == str(_new(runtime))
    assert runtime_env.leftover_runtime_dirs() == [runtime / "venv"]


def test_a_failed_upgrade_leaves_the_old_runtime_active_and_untouched(runtime):
    _mark_outdated(runtime)
    marker = (runtime / "ready.json").read_text()
    with pytest.raises(runtime_env.RuntimeInstallError, match="Installing cognee with uv"):
        _install(runtime, fail_on="install")
    assert (runtime / "ready.json").read_text() == marker
    assert runtime_env.outdated_requirement() == "cognee==0.0.1"
    assert runtime_env.server_python() == str(_legacy(runtime))
    assert (runtime / "venv" / "site.txt").read_text() == "old cognee"
    assert runtime_env.last_failure()["error"].startswith("Installing cognee with uv")


def test_a_venv_that_fails_verification_is_not_switched_to(runtime):
    _mark_outdated(runtime)
    with pytest.raises(runtime_env.RuntimeInstallError, match="expected"):
        _install(runtime, version="0.0.2")
    assert runtime_env.outdated_requirement() == "cognee==0.0.1"
    with pytest.raises(runtime_env.RuntimeInstallError, match="uvicorn"):
        _install(runtime, missing=("uvicorn",))
    assert runtime_env.outdated_requirement() == "cognee==0.0.1"


def test_a_complete_pinned_venv_is_reused_not_rebuilt(runtime):
    # A build that verified but died before the marker moved.
    _mark_outdated(runtime)
    _make_venv(_new(runtime))
    _, run = _install(runtime)
    assert run.commands == []
    assert runtime_env.runtime_python() == str(_new(runtime))


def test_an_unfinished_pinned_venv_is_rebuilt_from_scratch(runtime):
    _make_venv(_new(runtime))
    (runtime / f"venv-{PINNED}" / "junk").write_text("half a build")
    run = _FakeRun(runtime)
    probes = iter([json.dumps({"cognee": "", "missing": ["cognee"]})])

    def fake(command, **kwargs):
        if command[-1] == runtime_env._VERIFY_PROBE and (out := next(probes, None)):
            return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")
        return run(command, **kwargs)

    with (
        mock.patch.object(runtime_env, "_find_uv", return_value="/bin/uv"),
        mock.patch.object(runtime_env.subprocess, "run", side_effect=fake),
    ):
        runtime_env.install_runtime(output=lambda _: None)
    assert not (runtime / f"venv-{PINNED}" / "junk").exists()
    assert [c[1] for c in run.commands] == ["venv", "pip"]


def test_the_marker_is_replaced_atomically(runtime):
    _mark_outdated(runtime)
    with mock.patch.object(runtime_env.os, "replace", wraps=os.replace) as replace:
        _install(runtime)
    assert any(Path(call.args[1]) == runtime / "ready.json" for call in replace.call_args_list)
    assert not list(runtime.glob(".ready.json.*"))


def test_a_quiet_install_captures_the_installer_output(runtime):
    def fail(command, **kwargs):
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.PIPE
        raise subprocess.CalledProcessError(2, command, stderr="Collecting\nNo network\n")

    with (
        mock.patch.object(runtime_env, "_find_uv", return_value="/bin/uv"),
        mock.patch.object(runtime_env.subprocess, "run", side_effect=fail),
    ):
        with pytest.raises(runtime_env.RuntimeInstallError, match="No network"):
            runtime_env.install_runtime(output=lambda _: None, quiet=True)


def test_a_failed_first_build_leaves_no_marker(runtime):
    with pytest.raises(runtime_env.RuntimeInstallError):
        _install(runtime, fail_on="install")
    assert not (runtime / "ready.json").exists()
    assert runtime_env.runtime_python() is None


def test_a_success_clears_the_recorded_failure(runtime):
    runtime.mkdir()
    runtime_env._record_failure("boom")
    _install(runtime)
    assert runtime_env.last_failure() == {}


# -- the install lock ---------------------------------------------------------


def test_an_interactive_install_waits_a_bounded_time_for_a_running_build(runtime, monkeypatch):
    monkeypatch.setattr(runtime_env, "LOCK_POLL_SECS", 0.01)
    lines = []
    with runtime_env._install_lock():
        with pytest.raises(runtime_env.RuntimeBusyError, match="still building"):
            runtime_env.install_runtime(output=lines.append, lock_wait=0.05)
    assert "Another process is building the cognee runtime" in lines[0]


def test_the_stager_exits_without_installing_while_the_lock_is_held(runtime):
    _mark_outdated(runtime)
    with (
        runtime_env._install_lock(),
        mock.patch.object(runtime_env.subprocess, "run") as run,
    ):
        assert runtime_env.build_in_progress() is True
        assert runtime_env.stage_runtime() == 0
    run.assert_not_called()
    assert runtime_env.build_in_progress() is False


def test_the_stager_builds_and_switches(runtime, capsys):
    _mark_outdated(runtime)
    run = _FakeRun(runtime)
    with (
        mock.patch.object(runtime_env, "_find_uv", return_value="/bin/uv"),
        mock.patch.object(runtime_env.subprocess, "run", side_effect=run),
    ):
        assert runtime_env.main(["stage"]) == 0
    assert runtime_env.runtime_python() == str(_new(runtime))
    assert (runtime / "venv" / "site.txt").exists()


def test_the_stager_never_does_a_first_build(runtime):
    with mock.patch.object(runtime_env.subprocess, "run") as run:
        assert runtime_env.stage_runtime() == 0
    run.assert_not_called()


def test_a_failed_build_holds_off_the_next_background_one_for_an_hour(runtime):
    _mark_outdated(runtime)
    run = _FakeRun(runtime, fail_on="install")
    with (
        mock.patch.object(runtime_env, "_find_uv", return_value="/bin/uv"),
        mock.patch.object(runtime_env.subprocess, "run", side_effect=run),
    ):
        assert runtime_env.stage_runtime() == 1
        assert runtime_env.stage_runtime() == 0  # held back: no second attempt
    assert len(run.commands) == 2  # venv + the failed install, once
    with mock.patch.object(runtime_env.subprocess, "Popen") as popen:
        assert _REAL_START_BACKGROUND_UPGRADE() is False
    popen.assert_not_called()
    # An hour later it is tried again.
    failure = json.loads((runtime / "last-failure.json").read_text())
    failure["failed_at"] = time.time() - runtime_env.UPGRADE_RETRY_SECS - 1
    (runtime / "last-failure.json").write_text(json.dumps(failure))
    with mock.patch.object(runtime_env.subprocess, "Popen") as popen:
        assert _REAL_START_BACKGROUND_UPGRADE() is True
    popen.assert_called_once()


def test_an_explicit_install_ignores_the_failure_backoff(runtime):
    _mark_outdated(runtime)
    runtime_env._record_failure("earlier")
    _install(runtime)
    assert runtime_env.runtime_python() == str(_new(runtime))


class _FakeMsvcrt:
    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self):
        self.held = False
        self.calls = []

    def locking(self, fd, mode, nbytes):
        self.calls.append(mode)
        if mode == self.LK_NBLCK:
            if self.held:
                raise OSError("locked")
            self.held = True
        else:
            self.held = False


def test_windows_locks_with_msvcrt(runtime, monkeypatch):
    fake = _FakeMsvcrt()
    monkeypatch.setattr(runtime_env, "fcntl", None)
    monkeypatch.setattr(runtime_env, "msvcrt", fake)
    with runtime_env._install_lock():
        assert fake.held
        assert runtime_env.build_in_progress() is True
        with pytest.raises(runtime_env.RuntimeBusyError):
            with runtime_env._install_lock(wait=0):
                pass
    assert not fake.held
    assert runtime_env.build_in_progress() is False
    assert fake.LK_NBLCK in fake.calls and fake.LK_UNLCK in fake.calls


# -- the background launch ----------------------------------------------------


def test_the_background_build_is_detached_and_logs_to_a_file(runtime):
    _mark_outdated(runtime)
    with mock.patch.object(runtime_env.subprocess, "Popen") as popen:
        assert _REAL_START_BACKGROUND_UPGRADE() is True
    argv, kwargs = popen.call_args.args[0], popen.call_args.kwargs
    assert argv[:2] == [sys.executable, "-c"]
    assert argv[3] == str(ROOT)
    if os.name == "nt":
        assert kwargs["creationflags"]
    else:
        assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is not None and kwargs["stdout"] is kwargs["stderr"]
    assert runtime_env.UPGRADE_LOG.exists()


def test_the_stager_command_imports_this_package_from_a_bare_interpreter(runtime, tmp_path):
    # -S -I: no site-packages and no PYTHONPATH, so only the stager's own
    # sys.path insert can find the package. main() is swapped for a probe.
    code = runtime_env._STAGER.replace("raise SystemExit(main(['stage']))", "print(main)")
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, runtime_env._package_parent()],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "function main" in result.stdout


def test_nothing_is_launched_while_a_build_runs(runtime):
    _mark_outdated(runtime)
    with runtime_env._install_lock(), mock.patch.object(runtime_env.subprocess, "Popen") as popen:
        assert _REAL_START_BACKGROUND_UPGRADE() is False
    popen.assert_not_called()


# -- server start -------------------------------------------------------------


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


def _spawn_once(**patches):
    """ensure_local_server against a down server that comes up after one spawn."""
    with (
        mock.patch.object(sb, "health_ok", side_effect=[False, True]),
        mock.patch.object(sb.subprocess, "Popen") as popen,
        mock.patch.object(sb.time, "sleep"),
        mock.patch.object(runtime_env, "has_cognee", return_value=False),
    ):
        sb.ensure_local_server(8011, log_path="/dev/null")
    return popen.call_args.args[0][0]


def test_an_outdated_runtime_starts_the_server_at_once_and_stages_in_the_background(
    runtime, monkeypatch
):
    _mark_outdated(runtime)
    started = mock.Mock(return_value=True)
    monkeypatch.setattr(runtime_env, "start_background_upgrade", started)
    with mock.patch.object(runtime_env.subprocess, "run") as run:
        python = _spawn_once()
    started.assert_called_once_with()
    run.assert_not_called()  # nothing installed in the foreground
    assert python == str(_legacy(runtime))
    assert runtime_env.outdated_requirement() == "cognee==0.0.1"


def test_a_failing_background_launch_still_starts_the_server(runtime, monkeypatch, caplog):
    _mark_outdated(runtime)
    monkeypatch.setattr(
        runtime_env, "start_background_upgrade", mock.Mock(side_effect=OSError("no fork"))
    )
    with caplog.at_level("WARNING", logger=sb.logger.name):
        python = _spawn_once()
    assert python == str(_legacy(runtime))
    assert "hermes cognee install-runtime" in caplog.text


def test_a_current_runtime_starts_without_staging(runtime, monkeypatch):
    _mark_ready(runtime, python=_new(runtime))
    started = mock.Mock()
    monkeypatch.setattr(runtime_env, "start_background_upgrade", started)
    assert _spawn_once() == str(_new(runtime))
    started.assert_not_called()


def test_a_running_server_is_reused_without_staging(runtime, monkeypatch):
    _mark_outdated(runtime)
    started = mock.Mock()
    monkeypatch.setattr(runtime_env, "start_background_upgrade", started)
    with (
        mock.patch.object(sb, "health_ok", return_value=True),
        mock.patch.object(sb.subprocess, "Popen") as popen,
    ):
        assert sb.ensure_local_server(8011) == "http://127.0.0.1:8011"
    started.assert_not_called()
    popen.assert_not_called()


# -- removal and status -------------------------------------------------------


def test_remove_runtime(runtime):
    assert runtime_env.remove_runtime() is False
    _mark_ready(runtime, python=_new(runtime))
    _make_venv(_legacy(runtime))
    assert runtime_env.remove_runtime() is True
    assert runtime_env.runtime_python() is None
    assert not (runtime / f"venv-{PINNED}").exists()
    assert not (runtime / "venv").exists()


def test_remove_runtime_keeps_old_venvs_while_a_server_runs(runtime):
    _mark_ready(runtime, python=_new(runtime))
    _make_venv(_legacy(runtime))
    assert runtime_env.remove_runtime(server_running=True) is True
    assert not (runtime / f"venv-{PINNED}").exists()
    assert (runtime / "venv").exists()


def _cli():
    from cognee_integration_hermes import cli

    return cli


def test_status_names_an_outdated_runtime(runtime):
    _mark_outdated(runtime)
    status = _cli()._runtime_status()
    assert status.startswith("outdated (cognee==0.0.1")
    assert "in the background the next time" in status
    assert "hermes cognee install-runtime" in status


def test_status_says_an_upgrade_is_running(runtime):
    _mark_outdated(runtime)
    with runtime_env._install_lock():
        status = _cli()._runtime_status()
    assert "upgrading to " + LOCAL_COGNEE_REQUIREMENT + " in the background" in status
    assert str(runtime_env.UPGRADE_LOG) in status


def test_status_reports_the_last_failure(runtime):
    _mark_outdated(runtime)
    runtime_env._record_failure("Installing cognee with uv failed (exit code 2)")
    status = _cli()._runtime_status()
    assert "failed at" in status
    assert "exit code 2" in status


def test_status_lists_old_runtimes(runtime, capsys):
    _mark_ready(runtime, python=_new(runtime))
    _make_venv(_legacy(runtime))
    cli = _cli()
    with (
        mock.patch.object(cli, "_provider_active", return_value=False),
        mock.patch.object(cli, "_update_hint", return_value=""),
    ):
        cli._print_status(mock.Mock(check_updates=False))
    out = capsys.readouterr().out
    assert "Local runtime:   ready" in out
    assert f"Old runtimes:    {runtime / 'venv'}" in out


def test_install_runtime_command_removes_old_runtimes_only_without_a_server(runtime, capsys):
    _mark_outdated(runtime)
    cli = _cli()
    run = _FakeRun(runtime)
    with (
        mock.patch.object(runtime_env, "_find_uv", return_value="/bin/uv"),
        mock.patch.object(runtime_env.subprocess, "run", side_effect=run),
        mock.patch.object(cli, "_local_server_running", return_value=True),
    ):
        assert cli._install_runtime(None) == 0
    assert (runtime / "venv").exists()
    assert "Kept the old runtime" in capsys.readouterr().out
    with mock.patch.object(cli, "_local_server_running", return_value=False):
        assert cli._install_runtime(None) == 0
    assert not (runtime / "venv").exists()
    assert runtime_env.runtime_python() == str(_new(runtime))
