"""The private Python environment the local cognee server runs from.

cognee cannot live in Hermes's own environment: Hermes resolves every plugin
into one shared lock, where cognee's pins conflict with Hermes extras and the
whole install is refused. So local mode keeps cognee in a venv of its own and
starts the server from it (``server_bootstrap._spawn``). The plugin itself
never imports cognee in that mode — it talks to the server over HTTP.

The venv is built only on request (``hermes cognee install-runtime``, or the
local branch of ``hermes memory setup``), never as a side effect of a session
starting: it downloads cognee and its dependencies from PyPI. It is built from
the interpreter running Hermes (cognee supports it: Hermes runs 3.14, cognee
3.10-3.14) with uv when one is on PATH, else stdlib ``venv`` + pip, and uv is
never allowed to download an interpreter. One venv serves every Hermes profile,
as the server it runs does (one port, one store under ~/.cognee).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from .backend import has_cognee
from .config import LOCAL_COGNEE_REQUIREMENT, SHARED_PLUGIN_STATE_DIR

try:  # POSIX only; on Windows two concurrent installs are not serialized.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None  # type: ignore[assignment]

RUNTIME_DIR = SHARED_PLUGIN_STATE_DIR / "hermes" / "runtime"
VENV_DIR = RUNTIME_DIR / "venv"
# Written only after cognee installed and its version was read back, so a build
# cut short leaves no marker and the next install starts over.
READY_MARKER = RUNTIME_DIR / "ready.json"
INSTALL_TIMEOUT_SECS = 1800

Output = Callable[[str], None]


class RuntimeInstallError(RuntimeError):
    """Building the runtime venv failed; the message says which step."""


def pinned_version() -> str:
    return LOCAL_COGNEE_REQUIREMENT.split("==", 1)[1]


def venv_python(venv_dir: Optional[Path] = None) -> Path:
    venv_dir = VENV_DIR if venv_dir is None else venv_dir
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def runtime_python() -> Optional[str]:
    """The runtime venv's interpreter, when it holds the pinned cognee."""
    try:
        marker = json.loads(READY_MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict) or marker.get("requirement") != LOCAL_COGNEE_REQUIREMENT:
        return None
    python = venv_python()
    return str(python) if python.exists() else None


def server_python() -> Optional[str]:
    """The interpreter to start the local server with, or None when there is none.

    The runtime venv first: it is the supported path and holds the pinned
    version. A cognee installed into Hermes's own environment by hand still
    works, as it did before the venv existed.
    """
    python = runtime_python()
    if python:
        return python
    return sys.executable if has_cognee() else None


def local_ready() -> bool:
    return server_python() is not None


def _find_uv() -> Optional[str]:
    return shutil.which("uv")


@contextlib.contextmanager
def _install_lock():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield
        return
    with open(RUNTIME_DIR / "install.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)


def _run(command: list[str], *, step: str, env: Optional[dict] = None, timeout: float) -> None:
    try:
        subprocess.run(command, env=env, check=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        raise RuntimeInstallError(f"{step} failed (exit code {exc.returncode})") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeInstallError(f"{step} did not finish within {int(timeout)}s") from exc
    except OSError as exc:
        raise RuntimeInstallError(f"{step} could not start: {exc}") from exc


def _installed_version(python: Path, timeout: float = 60) -> str:
    # Reads package metadata only: importing cognee would take seconds and
    # touch its data directories.
    probe = "import importlib.metadata as m; print(m.version('cognee'))"
    try:
        result = subprocess.run(
            [str(python), "-c", probe],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def install_runtime(output: Output = print, timeout: float = INSTALL_TIMEOUT_SECS) -> str:
    """Build (or update) the runtime venv; return its interpreter.

    Installer output streams straight to the terminal, so the user sees the
    download progress of a step that takes minutes. Concurrent calls wait for
    the first to finish and then find it ready.
    """
    with _install_lock():
        ready = runtime_python()
        if ready:
            output(f"cognee runtime is ready ({LOCAL_COGNEE_REQUIREMENT}): {VENV_DIR}")
            return ready
        with contextlib.suppress(OSError):
            READY_MARKER.unlink()
        python = venv_python()
        uv = _find_uv()
        output(f"Installing {LOCAL_COGNEE_REQUIREMENT} into {VENV_DIR}")
        output("This downloads cognee and its dependencies from PyPI and takes a few minutes.")
        if uv:
            env = {**os.environ, "UV_PYTHON_DOWNLOADS": "never"}
            if not python.exists():
                _run(
                    [uv, "venv", str(VENV_DIR), "--python", sys.executable],
                    step="Creating the venv with uv",
                    env=env,
                    timeout=timeout,
                )
            _run(
                [uv, "pip", "install", "--python", str(python), LOCAL_COGNEE_REQUIREMENT],
                step="Installing cognee with uv",
                env=env,
                timeout=timeout,
            )
        else:
            if not python.exists():
                _run(
                    [sys.executable, "-m", "venv", str(VENV_DIR)],
                    step="Creating the venv",
                    timeout=timeout,
                )
            _run(
                [str(python), "-m", "pip", "install", LOCAL_COGNEE_REQUIREMENT],
                step="Installing cognee with pip",
                timeout=timeout,
            )
        version = _installed_version(python)
        if version != pinned_version():
            raise RuntimeInstallError(
                f"the venv reports cognee {version or 'missing'}, expected {pinned_version()}"
            )
        READY_MARKER.write_text(
            json.dumps({"requirement": LOCAL_COGNEE_REQUIREMENT, "python": str(python)}),
            encoding="utf-8",
        )
        output(f"cognee runtime is ready ({LOCAL_COGNEE_REQUIREMENT}): {VENV_DIR}")
        return str(python)


def remove_runtime() -> bool:
    """Delete the runtime venv; True when there was one."""
    if not RUNTIME_DIR.exists():
        return False
    with _install_lock():
        with contextlib.suppress(OSError):
            READY_MARKER.unlink()
        shutil.rmtree(VENV_DIR, ignore_errors=True)
    return True
