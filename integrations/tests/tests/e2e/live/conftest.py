"""Fixtures for the live tier: real cognee server, real LLM, real graph.

Same driver as the rest of ``e2e/`` (hook scripts as subprocesses) but pointed at
a server the plugin boots itself, so nothing about the memory chain is faked.
Every test here is marked ``live`` and is therefore deselected by the default
``-m "not live"`` in addopts — ``pytest tests/`` stays hermetic and free.

Opt in with:

    COGNEE_RUN_LIVE=1 LLM_API_KEY=sk-... uv run pytest tests/e2e/live -m live

Isolation guarantees: a per-test HOME (so ``~/.cognee`` and ``~/.cognee-plugin``
both land in a temp tree), a pinned ephemeral port (never 8011, which a real
server usually owns), a per-test dataset, and a scrubbed environment so a
developer's exported COGNEE_* cannot redirect the run at a real server.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
from utils.live import (
    GraphClient,
    LiveSession,
    build_live_env,
    free_port,
    hook_events,
    reap_port,
    seed_plugin_venv,
    server_health,
)
from utils.suites import CLAUDE, Suite, state_dir

pytestmark = pytest.mark.live

#: How long SessionStart may take to boot a server (a seeded venv takes ~15s; an
#: unseeded one has to download and install cognee first).
BOOT_DEADLINE = 900.0


def _dump(label: str, body: str, limit: int = 1500) -> None:
    if body and body.strip():
        print(f"[live artifacts] {label}:\n{body[-limit:]}")


@pytest.fixture(scope="session")
def live_prereqs() -> str:
    """Skip the whole tier unless explicitly opted into, with a real LLM key."""
    if os.environ.get("COGNEE_RUN_LIVE", "").strip().lower() not in ("1", "true", "yes"):
        pytest.skip("live tier is opt-in: set COGNEE_RUN_LIVE=1")
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        pytest.skip("live tier needs a real LLM_API_KEY (cognify makes real LLM calls)")
    return key


@pytest.fixture
def live_suite() -> Suite:
    """Only claude-code for now; adding codex is a one-line change here."""
    return CLAUDE


@pytest.fixture
def live_home(live_suite: Suite, tmp_path: Path) -> Path:
    """A per-test HOME with the plugin venv seeded so boot is seconds, not minutes."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    seeded = seed_plugin_venv(home)
    if not seeded and os.environ.get("COGNEE_LIVE_ALLOW_BUILD", "") != "1":
        pytest.skip(
            "no host ~/.cognee-plugin/venv to seed; run the plugin once, or set "
            "COGNEE_LIVE_ALLOW_BUILD=1 to let the test build one (slow)"
        )
    return home


@pytest.fixture
def live_project(tmp_path: Path) -> Path:
    """The working directory the simulated session runs in."""
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    return project


@pytest.fixture
def live_dataset() -> str:
    """A dataset unique to this test, so runs never read each other's memory."""
    return f"live_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def nonce() -> str:
    """A token that cannot exist in any model's training data.

    Every live memory assertion hangs off one of these: asked about a nonce, a
    system with no memory cannot produce the right answer by confabulating, so a
    pass is evidence rather than coincidence.
    """
    return f"ZEPHYR-{uuid.uuid4().hex[:6].upper()}"


@pytest.fixture
def live_port() -> int:
    return free_port()


@pytest.fixture
def live_base_url(live_port: int) -> str:
    return f"http://127.0.0.1:{live_port}"


@pytest.fixture
def live_env(
    live_prereqs: str,
    live_suite: Suite,
    live_home: Path,
    live_project: Path,
    live_base_url: str,
    live_dataset: str,
) -> dict[str, str]:
    return build_live_env(
        home=live_home,
        project=live_project,
        base_url=live_base_url,
        dataset=live_dataset,
        llm_api_key=live_prereqs,
        suite=live_suite,
    )


@pytest.fixture
def live_session_factory(
    live_suite: Suite,
    live_home: Path,
    live_project: Path,
    live_env: dict[str, str],
    live_port: int,
    live_base_url: str,
):
    """Make simulated sessions; the first ``start()`` boots the real server.

    Teardown reaps whatever is still listening on the pinned port, so a run can
    never leave an orphan server behind.
    """
    created: list[LiveSession] = []

    def _make(name: str = "s1") -> LiveSession:
        session = LiveSession(
            suite=live_suite,
            home=live_home,
            project=live_project,
            env=live_env,
            session_id=f"live-{name}-{uuid.uuid4().hex[:8]}",
        )
        created.append(session)
        return session

    yield _make

    reaped = reap_port(live_port)
    if reaped:
        print(f"[live] reaped server pids on {live_port}: {reaped}")


@pytest.fixture
def started_session(live_session_factory, live_base_url: str, live_suite: Suite, live_home: Path):
    """A session whose SessionStart has run, with the server proven up.

    Asserting the server is really listening is not ceremony: if COGNEE_BASE_URL
    were unset the plugin would quietly run local-SDK mode, boot nothing, and
    every hook would still exit 0 — a live test that "passes" while testing the
    wrong backend.
    """

    def _start(name: str = "s1") -> LiveSession:
        session = live_session_factory(name)
        run = session.start()
        assert run.ok, f"SessionStart failed (rc={run.returncode}): {run.stderr[:800]}"

        modes = [d for e, d in hook_events(live_suite, live_home) if e == "endpoint_mode_selected"]
        assert modes, "no endpoint_mode_selected event — the plugin never chose a server endpoint"
        assert modes[-1].get("base_url") == live_base_url, (
            f"plugin targeted {modes[-1].get('base_url')!r}, not the pinned {live_base_url!r}"
        )

        deadline = time.monotonic() + BOOT_DEADLINE
        while time.monotonic() < deadline:
            if server_health(live_base_url) == 200:
                return session
            time.sleep(2.0)
        raise AssertionError(f"no healthy server on {live_base_url} within {BOOT_DEADLINE}s")

    return _start


@pytest.fixture
def graph(live_base_url: str, live_dataset: str, live_home: Path, live_suite: Suite) -> GraphClient:
    return GraphClient(
        base_url=live_base_url, dataset=live_dataset, home=live_home, suite=live_suite
    )


@pytest.fixture(autouse=True)
def live_artifacts(request, live_suite: Suite, live_home: Path, live_base_url: str):
    """On failure, dump the state that makes a live failure diagnosable.

    Without this a red live test says only "recall never returned X", which is
    unactionable — the answer is almost always in hook.log or recall-audit.log.

    Failure is detected via the session's counter rather than a
    ``pytest_runtest_makereport`` hook: the counter is incremented before
    teardown runs and needs no hook wiring to be correct.
    """
    failed_before = request.session.testsfailed
    yield
    if request.session.testsfailed == failed_before:
        return

    print(f"\n[live artifacts] home={live_home} base_url={live_base_url}")
    print(f"[live artifacts] server health: {server_health(live_base_url)}")

    events = hook_events(live_suite, live_home)
    print(f"[live artifacts] {len(events)} hook events; last 30:")
    for event, detail in events[-30:]:
        print(f"    {event}: {str(detail)[:220]}")

    # Why a recall came back empty is almost always here: which scopes were
    # dispatched, what each returned, and whether the 4s budget cut them off.
    recall_events = [
        (e, d)
        for e, d in events
        if e.startswith(("recall", "context_lookup")) or "budget" in e or "scope" in e
    ]
    if recall_events:
        print("[live artifacts] recall-related events:")
        for event, detail in recall_events:
            print(f"    {event}: {str(detail)[:300]}")

    for name in ("last_recall.json", "recall-audit.log", "exit-watcher.log", "hook.log"):
        path = state_dir(live_suite, live_home) / name
        if path.exists():
            _dump(name, path.read_text(encoding="utf-8", errors="replace"))
