"""Pytest fixtures wiring the shared infrastructure together.

Registered as a plugin by ``integrations/tests/conftest.py``. Tests written in
later tasks consume these fixtures; task 1 ships only the harness.

Key fixtures:
  - ``suite``            : parametrized over claude-code and codex
  - ``temp_home``        : isolated HOME dir for the test (nothing hits real ~)
  - ``project_dir``      : isolated working dir (the hook ``cwd``)
  - ``mock_server``      : running MockCogneeServer (ephemeral port)
  - ``run_hook``         : run a hook script as a subprocess (end-to-end)
  - ``isolated_modules`` : import a suite's config/_plugin_common in-process (unit)
  - ``payloads``         : the payload-builder module
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from utils import payloads as _payloads
from utils.isolation import ISOLATED_MODULES, load_suite_module
from utils.isolation import run_hook as _run_hook
from utils.mock_cognee import MockCogneeServer
from utils.suites import ALL_SUITES, Suite


@pytest.fixture(params=ALL_SUITES, ids=lambda s: s.name)
def suite(request) -> Suite:
    """Run the test once per integration suite (claude-code, codex)."""
    return request.param


@pytest.fixture
def temp_home(tmp_path: Path) -> Path:
    """A per-test HOME. All plugin state (~/.cognee-plugin) lands under here."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A per-test working directory used as the hook ``cwd``."""
    d = tmp_path / "project"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def mock_server():
    """A running mock Cognee server on an ephemeral free port.

    Starts before the test and is torn down afterwards even on failure (yield
    fixture finalization).
    """
    server = HTTPServer(host="localhost", port=0)
    server.start()
    try:
        yield MockCogneeServer(server)
    finally:
        server.stop()


@pytest.fixture
def payloads():
    """The synthetic stdin payload-builder module."""
    return _payloads


@pytest.fixture
def run_hook(temp_home: Path, project_dir: Path):
    """Return a callable that runs a hook script as a subprocess (end-to-end).

    Usage: ``run_hook(suite, "session-start.py", stdin=payloads.session_start(),
    service_url=mock_server.url)``.
    """

    def _call(
        suite: Suite,
        script: str,
        *args: str,
        stdin=None,
        service_url: str | None = None,
        api_key: str | None = "test-api-key",
        env: dict | None = None,
        timeout: float = 30.0,
        python: str | None = None,
        cwd: Path | None = None,
    ):
        return _run_hook(
            suite,
            script,
            *args,
            stdin=stdin if stdin is not None else "",
            home=temp_home,
            cwd=cwd if cwd is not None else project_dir,
            service_url=service_url,
            api_key=api_key,
            extra_env=env,
            timeout=timeout,
            python=python,
        )

    return _call


@pytest.fixture
def isolated_modules(temp_home: Path, monkeypatch):
    """Return a loader that imports a suite's module under isolated HOME (unit).

    Usage: ``config = isolated_modules(suite, "config")``. The module's dir
    constants resolve into ``temp_home`` and are restored after the test.
    """

    def _load(suite: Suite, name: str):
        return load_suite_module(suite, name, home=temp_home, monkeypatch=monkeypatch)

    yield _load

    # Drop the freshly-imported copies so they don't leak into other tests.
    for mod_name in ISOLATED_MODULES:
        sys.modules.pop(mod_name, None)


@pytest.fixture
def assert_clean_real_home():
    """Guard: assert the real ~/.cognee-plugin was not created/modified by a test.

    Use as a sanity check in isolation tests. Records the real dir's existence
    before the test and asserts it is unchanged after.
    """
    real = Path.home() / ".cognee-plugin"
    existed = real.exists()
    before = sorted(p.name for p in real.iterdir()) if existed else None
    yield
    now_exists = real.exists()
    after = sorted(p.name for p in real.iterdir()) if now_exists else None
    assert (existed, before) == (now_exists, after), (
        "real ~/.cognee-plugin was modified by the test — isolation leak"
    )
