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
    cloud_api_key,
    cloud_base_url,
    delete_test_datasets,
    free_port,
    hook_events,
    reap_port,
    seed_plugin_venv,
    server_health,
)
from utils.suites import ALL_SUITES, Suite, state_dir

pytestmark = pytest.mark.live

#: How long SessionStart may take to boot a server (a seeded venv takes ~15s; an
#: unseeded one has to download and install cognee first).
BOOT_DEADLINE = 900.0


def _dump(label: str, body: str, limit: int = 1500) -> None:
    if body and body.strip():
        print(f"[live artifacts] {label}:\n{body[-limit:]}")


def pytest_collection_modifyitems(config, items):
    """Drop ``local_only`` scenarios when the backend is a remote tenant.

    Enforced here rather than by a ``-m`` expression in the CI command, because
    these are the scenarios that kill the server: relying on the caller to pass the
    right marker expression means one forgotten flag points eight server-killing
    tests at a real tenant. Deselecting automatically makes the safe thing the
    default, including for anyone running the tier by hand with a tenant URL set.
    """
    if not cloud_base_url():
        return
    keep, drop = [], []
    for item in items:
        (drop if item.get_closest_marker("local_only") else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
        print(f"[live cloud] deselected {len(drop)} local-only scenario(s) (they kill the server)")


@pytest.fixture(scope="session")
def live_backend() -> str:
    """``"cloud"`` when a tenant URL is configured, else ``"local"``.

    One env var picks the backend for the whole tier — there is no separate cloud
    copy of these tests. The plugin needs no help either way: it boots a server only
    for a local URL (``will_boot = (not server_live) and _is_local_url(...)``), so a
    remote URL simply makes it connect.
    """
    return "cloud" if cloud_base_url() else "local"


@pytest.fixture(scope="session")
def live_prereqs(live_backend: str) -> str:
    """Skip the whole tier unless explicitly opted into, with the keys it needs.

    ``LLM_API_KEY`` is required only for the local backend, where cognify runs in
    the server this machine booted. Against cloud the LLM is the tenant's own
    concern, so demanding a key here would be asking for a credential the run never
    uses.
    """
    if os.environ.get("COGNEE_RUN_LIVE", "").strip().lower() not in ("1", "true", "yes"):
        pytest.skip("live tier is opt-in: set COGNEE_RUN_LIVE=1")

    if live_backend == "cloud":
        if not cloud_api_key():
            pytest.skip("cloud backend needs COGNEE_LIVE_API_KEY for the tenant")
        return os.environ.get("LLM_API_KEY", "").strip()

    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        pytest.skip("live tier needs a real LLM_API_KEY (cognify makes real LLM calls)")
    return key


@pytest.fixture(scope="session", autouse=True)
def cloud_tenant_is_clean(live_backend: str, live_prereqs: str):
    """Remove this tier's own datasets either side of the run.

    Local runs need nothing — the datasets live under a temp HOME that pytest
    deletes. On cloud they persist, and every test invents its own
    ``live_<uuid>``, so a nightly would pile them up indefinitely.

    Scoped to the ``live_`` prefix, never delete-all: the tenant may hold real
    data, and cleanup that is broader than the thing it cleans up is a much worse
    failure than the bloat it prevents.

    Cleaning on the way *in* as well as out matters: a run killed mid-way (timeout,
    cancelled job) never reaches its teardown, so the next run clears whatever it
    left. Session-scoped rather than per-test on purpose — the final sync happens
    in a detached worker, and deleting between tests would race a write that is
    still in flight.
    """
    if live_backend != "cloud":
        yield
        return

    base_url, api_key = cloud_base_url(), cloud_api_key()

    def _clean(when: str) -> None:
        deleted, failures = delete_test_datasets(base_url, api_key)
        if failures:
            # Never fail the run over cleanup — a red tier should mean the product
            # broke, not that teardown had a bad minute. But say so loudly: a
            # cleanup that quietly no-ops is exactly how a tenant fills up, which
            # is the problem this fixture exists to prevent.
            print(
                f"[live cloud] WARNING: {when} cleanup deleted {deleted} but FAILED on "
                f"{len(failures)}: {failures[:5]} — left behind on {base_url}"
            )
        else:
            print(f"[live cloud] {when} cleanup: deleted {deleted} test dataset(s)")

    _clean("pre-run")
    yield
    _clean("post-run")


@pytest.fixture(params=ALL_SUITES, ids=lambda s: s.name)
def live_suite(request) -> Suite:
    """Every scenario runs against every host integration.

    This multiplies the tier's wall-clock and LLM spend, which is the point:
    host seams can diverge in ways a mock cannot show, so a real graph is the
    only place those differences become visible.
    """
    return request.param


@pytest.fixture
def live_home(tmp_path: Path, live_backend: str) -> Path:
    """A per-test HOME; on the local backend the plugin venv is seeded into it.

    Deliberately suite-agnostic: the venv and ``~/.cognee`` are shared, and both
    suites keep their own state subdirectory beneath it. Cross-suite tests need one
    HOME holding both, so this must not depend on ``live_suite``.

    The cloud backend needs no venv at all. ``ensure_cognee_ready`` returns after an
    HTTP ``/health`` check when a base_url is configured — the ``import cognee``
    lives in the local-SDK branch below it — so the hooks talk to the tenant over
    stdlib HTTP and never load the package. Skipping the seed is what makes the
    cloud job the fast one: no venv build, no cache step.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    if live_backend == "cloud":
        return home

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
def live_port(live_backend: str) -> int:
    """The local port the plugin boots on; 0 when the backend is remote.

    0 is a deliberate sentinel rather than the cloud URL's port: teardown reaps
    whatever holds this port, and reaping the *cloud* port would kill an unrelated
    local process listening on 443. See ``live_session_factory``.
    """
    return 0 if live_backend == "cloud" else free_port()


@pytest.fixture
def live_base_url(live_backend: str, live_port: int) -> str:
    """The one fixture that decides which backend the whole tier runs against."""
    return cloud_base_url() if live_backend == "cloud" else f"http://127.0.0.1:{live_port}"


@pytest.fixture
def live_api_key(live_backend: str) -> str:
    """The tenant key for cloud; empty locally, where the plugin mints its own."""
    return cloud_api_key() if live_backend == "cloud" else ""


@pytest.fixture
def live_env(
    live_prereqs: str,
    live_suite: Suite,
    live_home: Path,
    live_project: Path,
    live_base_url: str,
    live_dataset: str,
    live_api_key: str,
) -> dict[str, str]:
    return build_live_env(
        home=live_home,
        project=live_project,
        base_url=live_base_url,
        dataset=live_dataset,
        llm_api_key=live_prereqs,
        suite=live_suite,
        api_key=live_api_key,
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
    """Make simulated sessions; on the local backend the first ``start()`` boots
    the real server.

    Teardown reaps whatever is still listening on the pinned port, so a run can
    never leave an orphan server behind — but ONLY for a server this machine
    booted. ``reap_port`` is ``lsof -ti tcp:<port>`` piped into ``kill``, so
    running it for a remote backend would kill whichever local process happens to
    hold the cloud URL's port (443, typically something of the developer's).
    ``live_port`` is 0 on cloud precisely so this cannot be reached by accident.
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

    if live_port:
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
def graph(live_base_url: str, live_dataset: str, live_home: Path) -> GraphClient:
    return GraphClient(base_url=live_base_url, dataset=live_dataset, home=live_home)


@pytest.fixture
def session_for(
    live_prereqs: str,
    live_home: Path,
    live_project: Path,
    live_base_url: str,
    live_dataset: str,
    live_port: int,
):
    """Boot a session for an *explicitly named* suite, sharing one server.

    For cross-suite scenarios, which cannot use ``started_session``: that rides
    the ``live_suite`` parametrization, so a writer/reader pair built from it would
    always be the same integration on both sides. Here the caller names each side.

    Every session shares the HOME, port, and dataset — so the graph is shared while
    each suite keeps its own state subdirectory, which is exactly the arrangement
    the shared-brain claim rests on.
    """

    def _make(suite: Suite, name: str, *, start: bool = True) -> LiveSession:
        session = LiveSession(
            suite=suite,
            home=live_home,
            project=live_project,
            env=build_live_env(
                home=live_home,
                project=live_project,
                base_url=live_base_url,
                dataset=live_dataset,
                llm_api_key=live_prereqs,
                suite=suite,
            ),
            session_id=f"live-{name}-{uuid.uuid4().hex[:8]}",
        )
        if not start:
            return session

        run = session.start()
        assert run.ok, f"{suite.name} SessionStart failed (rc={run.returncode}): {run.stderr[:800]}"
        deadline = time.monotonic() + BOOT_DEADLINE
        while time.monotonic() < deadline:
            if server_health(live_base_url) == 200:
                return session
            time.sleep(2.0)
        raise AssertionError(f"no healthy server on {live_base_url} within {BOOT_DEADLINE}s")

    yield _make

    # Local backend only — see live_session_factory for why reaping a remote
    # backend's port would kill an unrelated local process.
    if live_port:
        reaped = reap_port(live_port)
        if reaped:
            print(f"[live] reaped server pids on {live_port}: {reaped}")


@pytest.fixture(autouse=True)
def live_artifacts(request, live_home: Path, live_base_url: str):
    """On failure, dump the state that makes a live failure diagnosable.

    Without this a red live test says only "recall never returned X", which is
    unactionable — the answer is almost always in hook.log or recall-audit.log.

    Failure is detected via the session's counter rather than a
    ``pytest_runtest_makereport`` hook: the counter is incremented before
    teardown runs and needs no hook wiring to be correct.

    Deliberately does NOT depend on ``live_suite``. As an autouse fixture it would
    otherwise force the suite parametrization onto every test in this directory,
    including the cross-suite ones that need two named suites at once. Iterating
    the suites instead also makes it strictly more useful: a cross-suite failure
    dumps both sides, and for a single-suite test the other suite's state dir was
    never created, so nothing extra is printed.
    """
    failed_before = request.session.testsfailed
    yield
    if request.session.testsfailed == failed_before:
        return

    print(f"\n[live artifacts] home={live_home} base_url={live_base_url}")
    print(f"[live artifacts] server health: {server_health(live_base_url)}")

    for suite in ALL_SUITES:
        if not state_dir(suite, live_home).exists():
            continue  # this suite never ran in this test
        print(f"\n[live artifacts] ── {suite.name} ──")

        events = hook_events(suite, live_home)
        print(f"[live artifacts] {len(events)} hook events; last 30:")
        for event, detail in events[-30:]:
            print(f"    {event}: {str(detail)[:220]}")

        # Why a recall came back empty is almost always here: which scopes were
        # dispatched, what each returned, and whether the budget cut them off.
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
            path = state_dir(suite, live_home) / name
            if path.exists():
                _dump(f"{suite.name}/{name}", path.read_text(encoding="utf-8", errors="replace"))
