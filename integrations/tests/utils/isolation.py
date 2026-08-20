"""State isolation: redirect all plugin state into a per-test temporary HOME.

The hook scripts derive their config/state dirs from ``Path.home()`` as
**import-time constants** (``~/.cognee-plugin[/...]`` in config.py /
_plugin_common.py, ``~/.cognee`` in _env_file.py / _plugin_common.py), resolved
from ``$HOME`` (POSIX) / ``USERPROFILE`` (Windows). So isolation must set HOME
*before* those constants are bound:

  - end-to-end (subprocess): set HOME in the child's env — done here in ``run_hook``.
  - unit (in-process import): set HOME, then import the module fresh — done here in
    ``load_suite_module``.

Nothing ever touches the real ~/.cognee-plugin or ~/.cognee.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from .suites import Suite

#: Module names the suites expose under their scripts dir. All carry import-time
#: state (dir constants, env reads), so they are popped/reloaded together.
ISOLATED_MODULES = (
    "config",
    "_plugin_common",
    "_cognee_client",
    "_env_file",
    "_proc",
    "_recall_http",
    "_remember_http",
    "cognee_plugin",
    "cognee_statusline_render",
    "doctor",
)

#: Env vars that make hook subprocesses deterministic in tests:
#:   - COGNEE_PLUGIN_IN_VENV=1 disables _plugin_common's import-time
#:     _reexec_into_venv() (os.execv into ~/.cognee-plugin/venv — under pytest
#:     that would re-exec the whole test session);
#:   - COGNEE_IDLE_DISABLED suppresses the background idle/exit watcher spawns
#:     (store-user-prompt.py) whose extra requests are timing-dependent;
#:   - COGNEE_UPDATE_CHECK=off stops the update checker from calling
#:     raw.githubusercontent.com;
#:   - COGNEE_LAZY_BOOTSTRAP=0 makes SessionStart bootstrap synchronously
#:     instead of via a detached worker that can outlive the test.
#:   - PYTHONIOENCODING=utf-8 pins the child's stdio encoding: on Windows the
#:     default is the locale code page (cp1252), and while the renderers
#:     reconfigure their own stdout to UTF-8, other hook scripts don't — this
#:     keeps every child's output UTF-8 so run_hook can decode it as such.
#:     Tests that probe encoding behavior override it via extra_env/build_env.
DETERMINISTIC_ENV = {
    "COGNEE_PLUGIN_IN_VENV": "1",
    "COGNEE_IDLE_DISABLED": "1",
    "COGNEE_UPDATE_CHECK": "off",
    "COGNEE_LAZY_BOOTSTRAP": "0",
    "PYTHONIOENCODING": "utf-8",
}

#: Env-var prefixes scrubbed from the inherited environment so a developer's
#: exported COGNEE_*/LLM_* configuration can never leak into a test.
_SCRUBBED_PREFIXES = ("COGNEE_", "LLM_", "CLAUDE_", "CODEX_", "AGY_")


def scrub_env_dict(env: dict[str, str]) -> dict[str, str]:
    """Return ``env`` without any developer-exported Cognee/plugin variables."""
    return {k: v for k, v in env.items() if not k.startswith(_SCRUBBED_PREFIXES)}


def scrub_cognee_env(monkeypatch) -> None:
    """Delete all Cognee and host-integration vars for one test (in-process)."""
    for key in list(os.environ):
        if key.startswith(_SCRUBBED_PREFIXES):
            monkeypatch.delenv(key, raising=False)


def build_env(
    suite: Suite,
    home: Path | str,
    *,
    service_url: str | None = None,
    api_key: str | None = None,
    cwd: Path | str | None = None,
    deterministic: bool = True,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a subprocess environment with HOME redirected into ``home``.

    ``service_url`` is injected as ``COGNEE_BASE_URL`` (the var config.py's
    _ENV_MAP reads) and also as ``COGNEE_PLATFORM_API_URL`` so billing calls
    (credits-refresh) can never escape to the real cloud platform API. When
    ``service_url`` is None, COGNEE_BASE_URL is genuinely absent (local mode) —
    inherited COGNEE_*/LLM_* exports are always scrubbed first.
    """
    env = scrub_env_dict(dict(os.environ))
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows
    env[suite.cwd_env] = str(cwd if cwd is not None else home)
    if deterministic:
        env.update(DETERMINISTIC_ENV)
    if service_url is not None:
        env["COGNEE_BASE_URL"] = service_url
        env.setdefault("COGNEE_PLATFORM_API_URL", service_url)
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
        # Explicit UTF-8, not text=True: text mode decodes with the parent's
        # locale encoding — cp1252 on Windows — which mangles the UTF-8 the
        # hooks write (the bar's ·/✕ glyphs came back as mojibake and full-line
        # assertions failed). PYTHONIOENCODING (DETERMINISTIC_ENV) pins the
        # child's write side to match; errors="replace" keeps a stray
        # non-UTF-8 byte from crashing the harness instead of the assertion.
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def load_suite_module(
    suite: Suite,
    name: str,
    *,
    home: Path | str,
    monkeypatch,
) -> ModuleType:
    """Import a suite module (see ``ISOLATED_MODULES``) under isolated HOME.

    The module's dir constants are re-evaluated against ``home`` because the
    import happens after HOME is set. The ``monkeypatch`` fixture handles env and
    sys.path restoration; ``sys.modules`` entries are popped before import so each
    call binds to the requested suite.
    """
    if name not in ISOLATED_MODULES:
        raise ValueError(f"unknown suite module {name!r}; expected one of {ISOLATED_MODULES}")

    _isolate_process_env(home, monkeypatch)
    # Suite scripts dir must resolve first so sibling imports (e.g. `import config`)
    # bind to this suite. monkeypatch.syspath_prepend restores on teardown.
    monkeypatch.syspath_prepend(str(suite.scripts_dir))

    # Drop any previously-imported copy so the constants re-evaluate with the new
    # HOME and from this suite's directory.
    for mod_name in ISOLATED_MODULES:
        sys.modules.pop(mod_name, None)

    module = importlib.import_module(name)
    return importlib.reload(module)


def load_hook_module(
    suite: Suite,
    script: str,
    *,
    home: Path | str,
    monkeypatch,
) -> ModuleType:
    """Load a hyphen-named hook script (e.g. ``exit-watcher.py``) as a module.

    Hook scripts can't be imported by name; they are exec'd from their file
    path under a unique per-suite module name, with the suite's scripts dir
    first on sys.path so their sibling imports (``config``, ``_plugin_common``)
    bind to the same suite and the same isolated HOME.
    """
    script_path = suite.scripts_dir / script
    if not script_path.exists():
        raise FileNotFoundError(f"{suite.name}: no such hook script: {script_path}")

    _isolate_process_env(home, monkeypatch)
    monkeypatch.syspath_prepend(str(suite.scripts_dir))
    for mod_name in ISOLATED_MODULES:
        sys.modules.pop(mod_name, None)

    mod_name = f"{suite.name}_{script_path.stem}".replace("-", "_")
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _isolate_process_env(home: Path | str, monkeypatch) -> None:
    """Scrub inherited COGNEE_*/LLM_* vars, then point HOME at the temp dir."""
    scrub_cognee_env(monkeypatch)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for key, value in DETERMINISTIC_ENV.items():
        monkeypatch.setenv(key, value)
