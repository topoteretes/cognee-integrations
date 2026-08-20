"""Pytest fixtures wiring the shared infrastructure together.

Registered as a plugin by ``integrations/tests/conftest.py``.

Key fixtures:
  - ``suite``            : parametrized over every available host integration
  - ``temp_home``        : isolated HOME dir for the test (nothing hits real ~)
  - ``project_dir``      : isolated working dir (the hook ``cwd``)
  - ``mock_server``      : running MockCogneeServer (ephemeral port)
  - ``run_hook``         : run a hook script as a subprocess (end-to-end)
  - ``isolated_modules`` : import a suite's script modules in-process (unit)
  - ``payloads``         : the payload-builder module
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from utils import payloads as _payloads
from utils.isolation import (
    ISOLATED_MODULES,
    load_hook_module,
    load_suite_module,
)
from utils.isolation import run_hook as _run_hook
from utils.mock_cognee import MockCogneeServer
from utils.suites import ALL_SUITES, Suite

#: API key run_hook injects by default; mock_server pre-marks it valid so the
#: happy path needs no per-test identity seeding.
DEFAULT_TEST_API_KEY = "test-api-key"


@pytest.fixture(params=ALL_SUITES, ids=lambda s: s.name)
def suite(request) -> Suite:
    """Run the test once per available host integration suite."""
    return request.param


@pytest.fixture
def temp_home(tmp_path: Path) -> Path:
    """A per-test HOME. All plugin state (~/.cognee-plugin, ~/.cognee) lands here."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A per-test working directory used as the hook ``cwd``."""
    d = tmp_path / "project"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="session")
def _http_server():
    """One HTTPServer for the whole session (stopping one costs ~0.5s).

    Per-test isolation comes from ``mock_server``, which clears the handlers and
    request log and re-registers the routes against fresh state.
    """
    server = HTTPServer(host="localhost", port=0)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_server(_http_server):
    """A mock Cognee server on an ephemeral free port, reset for this test.

    All handlers, recorded requests and identity state are fresh. The default
    run_hook API key is pre-seeded as valid.
    """
    _http_server.clear()
    mock = MockCogneeServer(_http_server)
    mock.identity.seed_api_key(DEFAULT_TEST_API_KEY)
    return mock


@pytest.fixture
def payloads():
    """The synthetic stdin payload-builder module."""
    return _payloads


@pytest.fixture
def statusline(suite, isolated_modules, monkeypatch):
    """The suite's status-line renderer, isolated under the per-test HOME.

    Every marker path in the renderer derives from ``Path.home()``, so the
    isolated import already points them inside the temp HOME — tests write real
    marker files at the module's own constants. COGNEE_BASE_URL is left unset
    (local mode) unless a test sets it.
    """
    return isolated_modules(suite, "cognee_statusline_render")


@pytest.fixture
def run_hook(temp_home: Path, project_dir: Path):
    """Return a callable that runs a hook script as a subprocess (end-to-end).

    Usage: ``run_hook(suite, "session-start.py", stdin=payloads.session_start(),
    service_url=mock_server.url)``. The URL is injected as COGNEE_BASE_URL.
    """

    def _call(
        suite: Suite,
        script: str,
        *args: str,
        stdin=None,
        service_url: str | None = None,
        api_key: str | None = DEFAULT_TEST_API_KEY,
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
def hook_module(temp_home: Path, monkeypatch):
    """Load a hyphen-named hook script (e.g. ``exit-watcher.py``) in-process.

    Usage: ``watcher = hook_module(suite, "exit-watcher.py")``. Same isolation
    guarantees as ``isolated_modules``; sibling imports bind to the suite.
    """
    loaded: list[str] = []

    def _load(suite: Suite, script: str):
        module = load_hook_module(suite, script, home=temp_home, monkeypatch=monkeypatch)
        loaded.append(module.__name__)
        return module

    yield _load

    for mod_name in loaded + list(ISOLATED_MODULES):
        sys.modules.pop(mod_name, None)


@pytest.fixture
def closed_port_url():
    """A base URL whose port has nothing listening — a genuine ECONNREFUSED.

    The mock server cannot produce "server absent"; binding a socket and
    closing it hands back a port that is (almost certainly) still free.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.fixture(scope="session")
def _platform_http_server():
    """A second session-scoped HTTPServer, on its own port (see _http_server)."""
    server = HTTPServer(host="localhost", port=0)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def platform_server(_platform_http_server):
    """A mock server acting as the cloud platform API (billing routes).

    A distinct host from ``mock_server``: point COGNEE_PLATFORM_API_URL here to
    prove billing calls target the platform API rather than the memory data
    plane (a per-tenant host, which has no billing routes).
    """
    _platform_http_server.clear()
    return MockCogneeServer(_platform_http_server)


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
