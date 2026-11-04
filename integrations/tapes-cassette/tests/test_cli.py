"""The one-shot ``cognee-tapes-sync`` command (cron-style sync).

It runs one pass of the same ``Syncer`` the service uses, reports the result,
and exits 0 (completed), 1 (failed) or 2 (busy: another process is syncing
against the same state file).
"""

import json

import httpx
import pytest
from cognee_integration_tapes_cassette import cli, ingest, tapes_client
from cognee_integration_tapes_cassette.config import load_env_file

from .conftest import make_tapes_transport, read_state


@pytest.fixture
def cli_env(config, monkeypatch, fake_cognee):
    """Point the command at the test config and a mocked tapes API."""
    monkeypatch.setenv("TAPES_BASE_URL", config.tapes_base_url)
    monkeypatch.setenv("COGNEE_TAPES_DATASET", config.dataset_name)
    monkeypatch.setenv("CASSETTE_STATE_PATH", str(config.state_path))
    monkeypatch.delenv("COGNEE_STORAGE_ROOT", raising=False)
    transport = {"value": make_tapes_transport()}
    real_client = tapes_client.TapesClient

    def fake_client(base_url):
        return real_client(base_url, client=httpx.AsyncClient(transport=transport["value"]))

    monkeypatch.setattr(tapes_client, "TapesClient", fake_client)
    return transport


def test_a_completed_sync_exits_0(cli_env, config, fake_cognee, capsys):
    assert cli.main(["--env-file", "/nonexistent"]) == cli.EXIT_COMPLETED
    out = capsys.readouterr().out
    assert out.startswith("completed: fetched 2, ingested 1, unchanged 0, skipped 1")
    assert len(fake_cognee.added) == 1
    assert read_state(config)["last_synced_at"]


def test_json_output_is_the_status_snapshot(cli_env, capsys):
    assert cli.main(["--json", "--env-file", "/nonexistent"]) == cli.EXIT_COMPLETED
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["state"] == "completed"
    assert snapshot["ingested"] == 1


def test_a_second_run_uses_the_checkpoint_and_state(cli_env, fake_cognee, capsys):
    cli.main(["--env-file", "/nonexistent"])
    capsys.readouterr()
    assert cli.main(["--full", "--env-file", "/nonexistent"]) == cli.EXIT_COMPLETED
    assert "unchanged 1" in capsys.readouterr().out
    assert len(fake_cognee.added) == 1  # the same session is not ingested twice


def test_a_failed_sync_exits_1(cli_env, capsys):
    cli_env["value"] = httpx.MockTransport(lambda request: httpx.Response(500))
    assert cli.main(["--env-file", "/nonexistent"]) == cli.EXIT_FAILED
    assert capsys.readouterr().out.startswith("failed:")


def test_busy_when_another_process_holds_the_run_lock(cli_env, config, fake_cognee, capsys):
    lock_path = config.state_path.with_name(config.state_path.name + ".lock")
    with ingest._run_lock(lock_path) as acquired:
        assert acquired
        assert cli.main(["--env-file", "/nonexistent"]) == cli.EXIT_BUSY
    assert capsys.readouterr().out.startswith("busy:")
    assert fake_cognee.added == []
    # The lock is gone with its holder: the next run goes ahead.
    assert cli.main(["--env-file", "/nonexistent"]) == cli.EXIT_COMPLETED


def test_the_run_lock_is_released_after_a_run(config):
    lock_path = config.state_path.with_name(config.state_path.name + ".lock")
    with ingest._run_lock(lock_path) as first:
        assert first
    with ingest._run_lock(lock_path) as second:
        assert second


def test_env_file_loads_settings_without_overriding_the_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "TAPES_BASE_URL=http://tapes.local:8081\n"
        'export COGNEE_TAPES_DATASET="team_sessions"\n'
        "CASSETTE_PORT='9911'\n"
        "EMPTY=\n"
        "LLM_API_KEY=from-file\n",
        encoding="utf-8",
    )
    for key in ("TAPES_BASE_URL", "COGNEE_TAPES_DATASET", "CASSETTE_PORT", "EMPTY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "from-shell")

    load_env_file(env_file)

    import os

    assert os.environ["TAPES_BASE_URL"] == "http://tapes.local:8081"
    assert os.environ["COGNEE_TAPES_DATASET"] == "team_sessions"
    assert os.environ["CASSETTE_PORT"] == "9911"
    assert "EMPTY" not in os.environ
    assert os.environ["LLM_API_KEY"] == "from-shell"


def test_a_missing_env_file_is_ignored():
    load_env_file("/nonexistent/.env")
