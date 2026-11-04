"""E2e: ``cognee-search.sh`` under auto recall (SDK-1393), for every hook suite.

Unless the user pinned a dataset, a graph search that names none sends none, so
the server picks the dataset. Shared memory's UUIDs name datasets too, so they
are dropped with it. When the server is unreachable the wrapper falls back to
the local cognee-cli, which follows the same rule: no ``-d`` under auto recall,
the active dataset when pinned.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from utils.isolation import build_env, usable_bash

BASH = usable_bash()

pytestmark = pytest.mark.skipif(
    BASH is None, reason="requires a working POSIX bash (wrapper is not run on Windows)"
)

WRITE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"
# Nothing listens here: the server-first request fails and the CLI answers.
UNREACHABLE = "http://127.0.0.1:9"


def _launch(suite, home, *, pinned: bool, shared: bool = False) -> dict:
    key = "host-auto-e2e"
    record = {
        "session_id": "active_session",
        "conn_uuid": "c1",
        "dataset": "agent_sessions",
        "host_key": key,
        "recall_pinned": pinned,
    }
    if shared:
        record.update({"dataset_id": WRITE_ID, "dataset_ids": [WRITE_ID, OTHER_ID]})
    sessions = home / ".cognee-plugin" / suite.state_subdir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{key}.json").write_text(json.dumps(record), encoding="utf-8")
    return {"COGNEE_SESSION_KEY": key}


def _run(suite, home, service_url, extra):
    script = suite.scripts_dir / "cognee-search.sh"
    env = build_env(suite, home, service_url=service_url, api_key="k", extra=extra)
    return subprocess.run(
        [BASH, str(script), "what did we decide", "5"],
        env=env,
        cwd=str(home),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


@pytest.mark.parametrize("pinned", [False, True])
def test_auto_drops_shared_memory_ids(suite, temp_home, mock_server, pinned):
    result = _run(
        suite,
        temp_home,
        mock_server.url,
        _launch(suite, temp_home, pinned=pinned, shared=True),
    )
    assert result.returncode == 0, result.stderr
    body = mock_server.assert_called("POST", "/api/v1/recall")["json"]
    assert body["scope"] == ["graph"]
    if pinned:
        assert body["dataset_ids"] == [WRITE_ID, OTHER_ID]
    else:
        assert "datasets" not in body and "dataset_ids" not in body
        # The session is bound to the active dataset, so it goes too.
        assert "session_id" not in body


@pytest.fixture
def fake_cli(tmp_path):
    """A ``cognee-cli`` on PATH logging its argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "cli-args.log"
    cli = bin_dir / "cognee-cli"
    cli.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\necho \'[]\'\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return {"log": log, "path": str(bin_dir)}


@pytest.mark.parametrize("pinned", [False, True])
def test_the_cli_fallback_follows_the_recall_mode(suite, temp_home, fake_cli, pinned):
    extra = _launch(suite, temp_home, pinned=pinned)
    extra["PATH"] = fake_cli["path"] + os.pathsep + os.environ.get("PATH", "")
    result = _run(suite, temp_home, UNREACHABLE, extra)
    assert "falling back to cognee-cli" in result.stderr, result.stderr
    (argv,) = fake_cli["log"].read_text(encoding="utf-8").splitlines()
    assert argv.startswith("recall what did we decide")
    if pinned:
        assert "-d agent_sessions" in argv
    else:
        assert "-d" not in argv.split()


# ── "auto" set only in ~/.cognee/.env ──────────────────────────────────────
#
# The wrappers' bash ``case`` only sees an exported COGNEE_PLUGIN_DATASET; the
# env file is loaded later, inside their Python, and must be normalised there
# too or a save/search names a dataset called "auto".


def _env_file(home, value: str) -> None:
    (home / ".cognee").mkdir(parents=True, exist_ok=True)
    (home / ".cognee" / ".env").write_text(f"COGNEE_PLUGIN_DATASET={value}\n", encoding="utf-8")


def test_search_with_auto_only_in_the_env_file_names_the_default_dataset(
    suite, temp_home, mock_server
):
    _env_file(temp_home, '" AUTO "')
    # Pinned, so the search names its dataset and the name is visible.
    result = _run(suite, temp_home, mock_server.url, {"COGNEE_PLUGIN_RECALL_MODE": "pinned"})
    assert result.returncode == 0, result.stderr
    body = mock_server.assert_called("POST", "/api/v1/recall")["json"]
    assert body["datasets"] == ["agent_sessions"]


def test_remember_with_auto_only_in_the_env_file_saves_to_the_default_dataset(
    suite, temp_home, mock_server
):
    _env_file(temp_home, '" AUTO "')
    env = build_env(
        suite,
        temp_home,
        service_url=mock_server.url,
        api_key="k",
        extra={"COGNEE_REMEMBER_WAIT_SECONDS": "0"},
    )
    result = subprocess.run(
        [BASH, str(suite.scripts_dir / "cognee-remember.sh"), "Yuna likes Yukki"],
        env=env,
        cwd=str(temp_home),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    form = mock_server.assert_called("POST", "/api/v1/remember")["form"]
    assert form.get("datasetName") == "agent_sessions"
