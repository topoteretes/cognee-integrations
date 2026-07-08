"""Opt-in end-to-end smoke: boot a throwaway local cognee server, remember a
tiny unique doc, then recall it and assert the token comes back.

This is the one test that proves the *real* remember -> cognify -> recall loop
against a live server — the failure mode users actually hit — as opposed to the
mocked-HTTP unit tests (test_remember_http.py / test_recall_http.py).

Run locally (needs cognee installed + LLM creds for the cognify write):
    COGNEE_RUN_INTEGRATION=1 LLM_API_KEY=sk-... uv run pytest tests/test_integration_smoke.py -v

Skipped by default (no COGNEE_RUN_INTEGRATION / cognee not installed), so it
stays green in CI without creds. This file is identical across the scripts-only
plugins (claude-code, codex): it locates the plugin's scripts/ dir itself, so it
can be copied verbatim — the "standardize on Hermes's approach" goal.
"""

import json
import os
import pathlib
import sys
import time
import uuid

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # _smoke_support (identical sibling module)

import _smoke_support as sup  # noqa: E402

# Put the plugin's scripts dir on the path so we can reuse its shipped HTTP
# clients (_remember_http / _recall_http) — the exact code paths production uses.
# Candidates cover both layouts: claude-code (scripts/) and codex (plugins/cognee/scripts/).
for _cand in (HERE.parent / "scripts", HERE.parent / "plugins" / "cognee" / "scripts"):
    if (_cand / "_remember_http.py").exists():
        sys.path.insert(0, str(_cand))
        break

pytestmark = pytest.mark.skipif(not (sup.RUN and sup.HAS_COGNEE), reason=sup.REASON)


@pytest.fixture
def live_server(tmp_path):
    """A throwaway local cognee server on a free port, with isolated data dirs.

    Yields the base URL; guarantees teardown of the spawned process.
    """
    port = sup.free_port()
    url = f"http://127.0.0.1:{port}"
    data_root = str(tmp_path / "data")
    system_root = str(tmp_path / "system")
    os.makedirs(data_root, exist_ok=True)
    os.makedirs(system_root, exist_ok=True)
    log_path = str(tmp_path / "server.log")

    proc = sup.spawn_server(port, data_root, system_root, log_path)
    try:
        if not sup.wait_healthy(url, deadline_s=90.0):
            tail = pathlib.Path(log_path).read_text(errors="replace")[-2000:]
            raise RuntimeError(
                f"cognee server did not become healthy at {url}\n"
                f"--- server log tail ---\n{tail}"
            )
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def test_remember_then_recall_roundtrip(live_server, monkeypatch):
    import _recall_http
    import _remember_http

    # Synchronous write so the graph is immediately queryable — no background poll race.
    monkeypatch.setenv("COGNEE_REMEMBER_BACKGROUND", "false")

    token = uuid.uuid4().hex[:12]
    dataset = f"smoke_{token}"
    content = f"Integration smoke probe {token}: the capital of Testland is {token}ville."

    result = _remember_http.do_remember(live_server, "", content, dataset, "smoke")
    assert result != _remember_http.UNREACHABLE, "server was unreachable during remember"
    assert not (isinstance(result, dict) and result.get("error")), f"remember failed: {result}"

    # Even after a synchronous write the vector index can lag a beat; retry briefly.
    hit = None
    for _ in range(5):
        results = _recall_http.do_recall(
            live_server, "", f"capital of Testland {token}", "", "", 5, dataset
        )
        assert results != _recall_http.UNREACHABLE, "server unreachable during recall"
        if isinstance(results, dict) and results.get("error"):
            raise AssertionError(f"recall errored: {results}")
        if token in json.dumps(results):
            hit = results
            break
        time.sleep(2.0)

    assert hit is not None, f"recall did not return the remembered token {token!r}"
