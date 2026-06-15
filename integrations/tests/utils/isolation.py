"""State isolation: redirect all plugin state into a per-test temporary HOME.

The hook scripts derive their config/state dir from ``Path.home()/".cognee-plugin"``
as **import-time constants**, resolved from ``$HOME`` (POSIX) / ``USERPROFILE``
(Windows). So isolation must set HOME *before* those constants are bound:

  - end-to-end (subprocess): set HOME in the child's env — done here in ``run_hook``.
  - unit (in-process import): set HOME, then import the module fresh — done here in
    ``load_suite_module``.

Nothing ever touches the real ~/.cognee-plugin.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from .suites import Suite

#: Module names the suites expose under their scripts dir.
ISOLATED_MODULES = ("config", "_plugin_common")


def build_env(
    suite: Suite,
    home: Path | str,
    *,
    service_url: str | None = None,
    api_key: str | None = None,
    cwd: Path | str | None = None,
    disable_idle_watcher: bool = True,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a subprocess environment with HOME redirected into ``home``."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows
    env[suite.cwd_env] = str(cwd if cwd is not None else home)
    if disable_idle_watcher:
        # Suppress the background idle/exit watcher subprocesses unless a test
        # specifically wants them (keeps request logs deterministic).
        env["COGNEE_IDLE_DISABLED"] = "1"
    if service_url is not None:
        env["COGNEE_SERVICE_URL"] = service_url
    if api_key is not None:
        env["COGNEE_API_KEY"] = api_key
    if extra:
        env.update(extra)
    return env


def run_hook(
    suite: Suite,
    script: str,
    *args: str,
    stdin: Any = "",
    home: Path | str,
    cwd: Path | str | None = None,
    service_url: str | None = None,
    api_key: str | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float = 30.0,
    python: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a hook script as a subprocess with isolated HOME and return the result.

    ``stdin`` may be a dict (JSON-serialized for you) or a str. ``python`` defaults
    to the interpreter running the tests.
    """
    script_path = suite.scripts_dir / script
    if not script_path.exists():
        raise FileNotFoundError(f"{suite.name}: no such hook script: {script_path}")

    if not isinstance(stdin, str):
        stdin = json.dumps(stdin)

    env = build_env(
        suite,
        home,
        service_url=service_url,
        api_key=api_key,
        cwd=cwd,
        extra=extra_env,
    )
    return subprocess.run(
        [python or sys.executable, str(script_path), *args],
        input=stdin,
        env=env,
        cwd=str(cwd if cwd is not None else home),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def load_suite_module(
    suite: Suite,
    name: str,
    *,
    home: Path | str,
    monkeypatch,
) -> ModuleType:
    """Import a suite module (``config`` / ``_plugin_common``) under isolated HOME.

    The module's dir constants are re-evaluated against ``home`` because the
    import happens after HOME is set. The ``monkeypatch`` fixture handles env and
    sys.path restoration; ``sys.modules`` entries are popped before import so each
    call binds to the requested suite.
    """
    if name not in ISOLATED_MODULES:
        raise ValueError(f"unknown suite module {name!r}; expected one of {ISOLATED_MODULES}")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Suite scripts dir must resolve first so sibling imports (e.g. `import config`)
    # bind to this suite. monkeypatch.syspath_prepend restores on teardown.
    monkeypatch.syspath_prepend(str(suite.scripts_dir))

    # Drop any previously-imported copy so the constants re-evaluate with the new
    # HOME and from this suite's directory.
    for mod_name in ISOLATED_MODULES:
        sys.modules.pop(mod_name, None)

    module = importlib.import_module(name)
    return importlib.reload(module)
