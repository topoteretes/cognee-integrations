"""The private Python environment the local cognee server runs from.

cognee cannot live in Hermes's own environment: Hermes resolves every plugin
into one shared lock, where cognee's pins conflict with Hermes extras and the
whole install is refused. So local mode keeps cognee in a venv of its own and
starts the server from it (``server_bootstrap._spawn``). The plugin itself
never imports cognee in that mode — it talks to the server over HTTP.

Each cognee pin gets a venv of its own, ``runtime/venv-<version>``, and the
ready marker (``ready.json``) names the interpreter the server starts from. A
venv is never changed once a server may run from it: a new pin is built next to
the old one, verified, and only then becomes the active runtime, by replacing
the marker in one atomic step. A server already running keeps its old venv; the
next one starts from the new. Plugins before 1.5.0 built ``runtime/venv`` and a
marker without a version in the path; that runtime still counts.

A build happens only on request (``hermes cognee install-runtime``, or the
local branch of ``hermes memory setup``), never as a side effect of a session
starting: it downloads cognee and its dependencies from PyPI. A runtime an older
plugin version built for another cognee pin does not count as installed: local
mode stays off, and says to run ``install-runtime``, until the current pin is
built. The plugin never starts a server on a cognee it was not written against.

Venvs are built from the interpreter running Hermes (cognee supports it: Hermes
runs 3.14, cognee 3.10-3.14) with uv when one is on PATH, else stdlib ``venv``
+ pip, and uv is never allowed to download an interpreter. One active venv
serves every Hermes profile, as the server it runs does (one port, one store
under ~/.cognee).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

from .backend import cognee_not_installed_message, has_cognee
from .config import LOCAL_COGNEE_REQUIREMENT, SHARED_PLUGIN_STATE_DIR

# The install lock: flock on POSIX, a byte-range lock on Windows. Only one of
# them is ever present; with neither, builds are not serialized.
try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]

RUNTIME_DIR = SHARED_PLUGIN_STATE_DIR / "hermes" / "runtime"
# Points at the active runtime: {"requirement": "cognee==X", "python": "..."}.
# Written only after a venv's cognee was verified, and replaced atomically, so
# it never names a half-built venv.
READY_MARKER = RUNTIME_DIR / "ready.json"
INSTALL_TIMEOUT_SECS = 1800
# How long an install waits for a build another process is running (another
# terminal, or the setup wizard) before giving up.
INSTALL_LOCK_WAIT_SECS = 900
LOCK_POLL_SECS = 1.0

Output = Callable[[str], None]


class RuntimeInstallError(RuntimeError):
    """Building the runtime venv failed; the message says which step."""


class RuntimeBusyError(RuntimeInstallError):
    """Another process holds the install lock (it is building the runtime)."""


def pinned_version() -> str:
    return LOCAL_COGNEE_REQUIREMENT.split("==", 1)[1]


def legacy_venv_dir() -> Path:
    """Where plugins before 1.5.0 built the one venv they had."""
    return RUNTIME_DIR / "venv"


def pinned_venv_dir() -> Path:
    """Where the current pin's venv is built."""
    return RUNTIME_DIR / f"venv-{pinned_version()}"


def venv_python(venv_dir: Optional[Path] = None) -> Path:
    venv_dir = pinned_venv_dir() if venv_dir is None else venv_dir
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_of(python: Path) -> Path:
    return python.parent.parent


def _read_marker() -> dict:
    try:
        marker = json.loads(READY_MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return marker if isinstance(marker, dict) else {}


def _marker_requirement() -> Optional[str]:
    requirement = _read_marker().get("requirement")
    return requirement if isinstance(requirement, str) else None


def _marker_python() -> Optional[Path]:
    """The interpreter the marker names (the legacy venv's for a marker without one)."""
    marker = _read_marker()
    if not marker:
        return None
    python = marker.get("python")
    if isinstance(python, str) and python:
        return Path(python)
    return venv_python(legacy_venv_dir())


def active_venv_dir() -> Optional[Path]:
    """The venv the marker points at, when it still exists."""
    python = _marker_python()
    return _venv_of(python) if python is not None and python.exists() else None


def runtime_python() -> Optional[str]:
    """The active runtime's interpreter, when it holds the pinned cognee."""
    if _marker_requirement() != LOCAL_COGNEE_REQUIREMENT:
        return None
    python = _marker_python()
    return str(python) if python is not None and python.exists() else None


def outdated_requirement() -> Optional[str]:
    """The ``cognee==X`` the active runtime holds when X is not the current pin.

    An earlier plugin version built that venv. It is not used: local mode stays
    off until ``install-runtime`` builds the current pin next to it.
    """
    requirement = _marker_requirement()
    if (
        not requirement
        or requirement == LOCAL_COGNEE_REQUIREMENT
        or not requirement.startswith("cognee==")
    ):
        return None
    python = _marker_python()
    return requirement if python is not None and python.exists() else None


def server_python() -> Optional[str]:
    """The interpreter to start the local server with, or None when there is none.

    The active runtime first, when it holds the pinned version: it is the
    supported path. A cognee installed into Hermes's own environment by hand
    still works, as it did before the venv existed. A runtime an older plugin
    built for another pin is never used.
    """
    python = runtime_python()
    if python:
        return python
    return sys.executable if has_cognee() else None


def local_ready() -> bool:
    return server_python() is not None


def local_not_ready_message() -> str:
    """What to tell a user whose local mode cannot start (no usable runtime)."""
    outdated = outdated_requirement()
    if not outdated:
        return cognee_not_installed_message("local")
    return (
        f"cognee local mode is off: its runtime holds {outdated}, and this plugin version "
        f"needs {LOCAL_COGNEE_REQUIREMENT}. Run `hermes cognee install-runtime` in a "
        "terminal to install it (it is built next to the old runtime, which stays as it "
        "is until the new one checks out), then start a new Hermes session."
    )


def leftover_runtime_dirs() -> list[Path]:
    """Runtime venvs the marker no longer points at (an old pin, a failed build).

    The current pin's directory is never listed: it is the active runtime, a
    build in progress, or a failed one the next build starts over in.
    """
    try:
        entries = sorted(RUNTIME_DIR.iterdir())
    except OSError:
        return []
    active = active_venv_dir()
    keep = {pinned_venv_dir()} | ({active} if active is not None else set())
    return [
        path
        for path in entries
        if path.is_dir()
        and (path.name == "venv" or path.name.startswith("venv-"))
        and path not in keep
    ]


def _write_json(path: Path, value: dict) -> None:
    # Write a sibling and swap it in, so a reader never sees half a file.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(value), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise RuntimeInstallError(f"Could not write {path}: {exc}") from exc


def _find_uv() -> Optional[str]:
    return shutil.which("uv")


def _try_lock(handle) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True
    if msvcrt is not None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    return True


def _unlock(handle) -> None:
    with contextlib.suppress(OSError):
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def _install_lock(wait: float = 0.0, on_wait: Optional[Output] = None) -> Iterator[None]:
    """Hold the install lock, waiting up to ``wait`` seconds for it.

    Raises :class:`RuntimeBusyError` when another process still holds it. It is
    an OS lock, so it is released when its holder dies, however that happens.
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUNTIME_DIR / "install.lock", "a+") as handle:
        if not _try_lock(handle):
            if on_wait is not None:
                on_wait(
                    "Another process is building the cognee runtime; "
                    f"waiting up to {int(wait)}s for it to finish."
                )
            deadline = time.monotonic() + wait
            while True:
                if time.monotonic() >= deadline:
                    raise RuntimeBusyError(
                        "another process is still building the cognee runtime; "
                        "retry once it has finished"
                    )
                time.sleep(LOCK_POLL_SECS)
                if _try_lock(handle):
                    break
        try:
            yield
        finally:
            _unlock(handle)


def build_in_progress() -> bool:
    """True while another process holds the install lock."""
    if (fcntl is None and msvcrt is None) or not RUNTIME_DIR.is_dir():
        return False
    try:
        with open(RUNTIME_DIR / "install.lock", "a+") as handle:
            if not _try_lock(handle):
                return True
            _unlock(handle)
    except OSError:
        return False
    return False


def _run(
    command: list[str],
    *,
    step: str,
    env: Optional[dict] = None,
    timeout: float,
    quiet: bool = False,
) -> None:
    # quiet: the installer's output is captured and only the last line of
    # stderr is kept, for the error.
    capture = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE, "text": True}
    try:
        subprocess.run(command, env=env, check=True, timeout=timeout, **(capture if quiet else {}))
    except subprocess.CalledProcessError as exc:
        lines = (exc.stderr or "").strip().splitlines() if quiet else []
        detail = f": {lines[-1]}" if lines else ""
        raise RuntimeInstallError(f"{step} failed (exit code {exc.returncode}){detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeInstallError(f"{step} did not finish within {int(timeout)}s") from exc
    except OSError as exc:
        raise RuntimeInstallError(f"{step} could not start: {exc}") from exc


# Reads package metadata and looks the server's modules up without importing
# them: importing cognee would take seconds and touch its data directories.
_VERIFY_PROBE = (
    "import importlib.metadata as m, importlib.util as u, json; "
    "print(json.dumps({'cognee': m.version('cognee'), "
    "'missing': [n for n in ('cognee', 'uvicorn') if u.find_spec(n) is None]}))"
)


def _verify(python: Path, timeout: float = 60) -> str:
    """Why the venv cannot run the server on the pinned cognee ("" when it can)."""
    try:
        result = subprocess.run(
            [str(python), "-c", _VERIFY_PROBE],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        found = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        found = {}
    if not isinstance(found, dict):
        found = {}
    version = found.get("cognee") or ""
    if version != pinned_version():
        return f"the venv reports cognee {version or 'missing'}, expected {pinned_version()}"
    missing = found.get("missing")
    if missing:
        return f"the venv cannot find {', '.join(missing)}"
    return ""


def _build(output: Output, timeout: float, quiet: bool) -> str:
    """Build the pinned venv and make it active; the install lock must be held.

    Only the pinned venv's own directory is written, and the marker is replaced
    only once that venv checks out, so a build cut short at any step leaves the
    active runtime exactly as it was. The pinned venv is always built from
    scratch: one the marker does not name yet may be a build cut short partway
    through its install, which the version check alone cannot tell apart from a
    complete one.
    """
    target = pinned_venv_dir()
    python = venv_python(target)
    # Nothing runs from it (the marker never pointed at it).
    shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        raise RuntimeInstallError(
            f"could not clear {target} left by an earlier build; delete it and retry"
        )
    uv = _find_uv()
    output(f"Installing {LOCAL_COGNEE_REQUIREMENT} into {target}")
    output("This downloads cognee and its dependencies from PyPI and takes a few minutes.")
    if uv:
        env = {**os.environ, "UV_PYTHON_DOWNLOADS": "never"}
        _run(
            [uv, "venv", str(target), "--python", sys.executable],
            step="Creating the venv with uv",
            env=env,
            timeout=timeout,
            quiet=quiet,
        )
        _run(
            [uv, "pip", "install", "--python", str(python), LOCAL_COGNEE_REQUIREMENT],
            step="Installing cognee with uv",
            env=env,
            timeout=timeout,
            quiet=quiet,
        )
    else:
        _run(
            [sys.executable, "-m", "venv", str(target)],
            step="Creating the venv",
            timeout=timeout,
            quiet=quiet,
        )
        _run(
            [str(python), "-m", "pip", "install", LOCAL_COGNEE_REQUIREMENT],
            step="Installing cognee with pip",
            timeout=timeout,
            quiet=quiet,
        )
    problem = _verify(python)
    if problem:
        raise RuntimeInstallError(problem)
    _write_json(READY_MARKER, {"requirement": LOCAL_COGNEE_REQUIREMENT, "python": str(python)})
    output(f"cognee runtime is ready ({LOCAL_COGNEE_REQUIREMENT}): {target}")
    return str(python)


def install_runtime(
    output: Output = print,
    timeout: float = INSTALL_TIMEOUT_SECS,
    *,
    quiet: bool = False,
    lock_wait: float = INSTALL_LOCK_WAIT_SECS,
) -> str:
    """Build the pinned runtime venv (if needed) and make it active; return its interpreter.

    Installer output streams straight to the terminal, so the user sees the
    download progress of a step that takes minutes; ``quiet`` captures it
    instead. A build another process is running is waited for up to
    ``lock_wait`` seconds (then :class:`RuntimeBusyError`), and found ready when
    it succeeded. An outdated runtime is left as it is: the new one is built
    next to it, and a failed build leaves the marker on it.
    """
    with _install_lock(wait=lock_wait, on_wait=output):
        ready = runtime_python()
        if ready:
            output(f"cognee runtime is ready ({LOCAL_COGNEE_REQUIREMENT}): {_venv_of(Path(ready))}")
            return ready
        return _build(output, timeout, quiet)


def remove_leftover_runtimes() -> list[Path]:
    """Delete the venvs the marker does not point at; return those deleted.

    The caller must know no server runs from them: a local server another
    profile started on a different port may still use the old one, so callers
    check the configured port only and the rest is the user's call. Skipped
    entirely while a build holds the install lock.
    """
    try:
        with _install_lock(wait=0):
            removed = leftover_runtime_dirs()
            for path in removed:
                shutil.rmtree(path, ignore_errors=True)
            return removed
    except RuntimeBusyError:
        return []


def remove_runtime(*, server_running: bool = False) -> bool:
    """Delete the active runtime venv; True when there was one.

    A server already running from it keeps running until it exits. Leftover
    venvs from earlier pins go too, unless ``server_running`` (a server on the
    configured port may have been started from one). Raises
    :class:`RuntimeBusyError` while a build holds the install lock.
    """
    if not RUNTIME_DIR.exists():
        return False
    with _install_lock(wait=0):
        active = active_venv_dir()
        # The pinned directory is the active runtime or an unfinished build.
        doomed = {pinned_venv_dir()} | ({active} if active is not None else set())
        if not server_running:
            doomed.update(leftover_runtime_dirs())
        existed = active is not None or any(path.exists() for path in doomed)
        with contextlib.suppress(OSError):
            READY_MARKER.unlink()
        for path in doomed:
            shutil.rmtree(path, ignore_errors=True)
    return existed
