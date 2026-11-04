"""Under shared memory the plugin never forks an agent-owned copy of its dataset.

The server's ``POST /api/v1/datasets`` create-or-return matches a name only
among the CALLER's datasets. The pre-improve ensure posted the launch's dataset
by name with the agent key, so on an agent that owned no dataset of that name
the server created an empty agent-owned ``agent_sessions`` beside the canonical,
parent-owned one SessionStart had resolved. The listing then marked both rows
current (it matched by name), and the Claude Code picker, offered the same name
twice, was refused by the engine and the whole status row vanished.

Covers, for every suite:
  * the ensure skips the by-name POST when the name has a canonical id, and
    still posts it when it has none (name-addressed launches)
  * ``switch-dataset.py --list`` marks only the launch's canonical row current
    when a same-named dataset of another principal is listed beside it
"""

from __future__ import annotations

import pytest

CANONICAL = "5708f668-e272-5475-9341-caac1f6ce9f9"
AGENT_COPY = "6eff268f-a66d-5d1c-a79d-fb4a1c44254e"


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


def _record_posts(pc, monkeypatch) -> list:
    posts: list = []
    monkeypatch.setattr(
        pc, "_json_http_request", lambda path, body, **kw: posts.append((path, body)) or {}
    )
    return posts


def test_ensure_skips_the_by_name_create_for_a_canonical_dataset(pc, monkeypatch):
    posts = _record_posts(pc, monkeypatch)
    monkeypatch.setattr(pc, "dataset_id_for", lambda name, host_key="": CANONICAL)
    pc.ensure_dataset_via_http("agent_sessions")
    assert posts == []


def test_ensure_still_creates_a_name_addressed_dataset(pc, monkeypatch):
    posts = _record_posts(pc, monkeypatch)
    monkeypatch.setattr(pc, "dataset_id_for", lambda name, host_key="": "")
    pc.ensure_dataset_via_http("agent_sessions")
    assert posts == [("/api/v1/datasets", {"name": "agent_sessions"})]


@pytest.fixture
def switch(suite, hook_module, monkeypatch):
    module = hook_module(suite, "switch-dataset.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(module, "load_resolved", lambda **kw: {"user_id": "agent"})
    monkeypatch.setattr(module, "recall_mode", lambda host_key: ("auto", ""))
    monkeypatch.setattr(
        module,
        "list_writable_datasets",
        lambda user_id="": {
            "datasets": [
                {"name": "agent_sessions", "id": CANONICAL, "writable": True},
                {"name": "agent_sessions", "id": AGENT_COPY, "writable": True},
                {"name": "demo", "id": "d2", "writable": True},
            ],
            "hidden_readonly": 0,
            "filtered": True,
        },
    )
    return module


def test_list_marks_only_the_canonical_row_current(switch):
    rec = {"dataset": "agent_sessions", "dataset_id": CANONICAL, "session_id": "s1"}
    rows = switch._list("host-1", rec)["datasets"]
    assert [row["id"] for row in rows if row["current"]] == [CANONICAL]


def test_list_without_a_canonical_id_matches_by_name(switch):
    rec = {"dataset": "demo", "session_id": "s1"}
    rows = switch._list("host-1", rec)["datasets"]
    assert [row["id"] for row in rows if row["current"]] == ["d2"]


# ── cognee-remember.sh --dataset ─────────────────────────────────────────────
# An explicit --dataset wins over the launch's dataset, and resolves as a
# dataset switch does: sent by name, the server would find or create an
# agent-owned copy beside the user's canonical one.

_RESOLVER_STUB = """
import os, sys
sys.path.insert(0, os.environ["STUB_SCRIPTS_DIR"])
import _plugin_common

def _resolve(name, **kw):
    with open(os.environ["STUB_RESOLVE_LOG"], "a") as fh:
        fh.write(name + "\\n")
    if os.environ["STUB_SHARED"] != "1":
        return {"mode": "separated", "dataset_id": ""}
    return {"mode": "shared", "dataset_id": os.environ["STUB_ID"]}

_plugin_common.resolve_shared_dataset = _resolve
"""


def _remember_into(suite, home, url, tmp_path, dataset, *, shared):
    import subprocess

    from utils.isolation import build_env, usable_bash

    bash = usable_bash()
    if bash is None:
        pytest.skip("requires a working POSIX bash")
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    (stub / "sitecustomize.py").write_text(_RESOLVER_STUB)
    log = tmp_path / "resolved.txt"
    log.touch()
    env = build_env(
        suite,
        home,
        service_url=url,
        api_key="k",
        extra={
            "COGNEE_REMEMBER_WAIT_SECONDS": "0",
            "PYTHONPATH": str(stub),
            "STUB_SCRIPTS_DIR": str(suite.scripts_dir),
            "STUB_RESOLVE_LOG": str(log),
            "STUB_SHARED": "1" if shared else "0",
            "STUB_ID": CANONICAL,
        },
    )
    result = subprocess.run(
        [bash, str(suite.scripts_dir / "cognee-remember.sh"), "note", "--dataset", dataset],
        env=env,
        cwd=str(home),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result, log.read_text().split()


def test_remember_dataset_flag_writes_to_the_canonical_copy(
    suite, temp_home, mock_server, tmp_path
):
    # The canonical copy belongs to the user, which the mock's identities do not model.
    mock_server.force_response("POST", "/api/v1/remember", 200, {"dataset_id": CANONICAL})
    result, resolved = _remember_into(
        suite, temp_home, mock_server.url, tmp_path, "demo", shared=True
    )
    assert resolved == ["demo"]
    form = mock_server.assert_called("POST", "/api/v1/remember")["form"]
    assert form.get("datasetId") == CANONICAL
    assert "datasetName" not in form
    assert '"dataset": "demo"' in result.stdout


def test_remember_dataset_flag_keeps_the_name_under_separated_memory(
    suite, temp_home, mock_server, tmp_path
):
    _, resolved = _remember_into(suite, temp_home, mock_server.url, tmp_path, "demo", shared=False)
    assert resolved == ["demo"]
    form = mock_server.assert_called("POST", "/api/v1/remember")["form"]
    assert form.get("datasetName") == "demo"
    assert "datasetId" not in form


def test_remember_dataset_flag_sends_a_uuid_as_is(suite, temp_home, mock_server, tmp_path):
    mock_server.force_response("POST", "/api/v1/remember", 200, {"dataset_id": AGENT_COPY})
    _, resolved = _remember_into(
        suite, temp_home, mock_server.url, tmp_path, AGENT_COPY, shared=True
    )
    # A UUID is never looked up as a name, which would create a dataset so named.
    assert resolved == []
    form = mock_server.assert_called("POST", "/api/v1/remember")["form"]
    assert form.get("datasetId") == AGENT_COPY
