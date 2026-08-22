"""Project-scoped datasets stay pinned across the full hook pipeline."""

from __future__ import annotations

import subprocess


def test_pipeline_uses_session_start_dataset(
    suite, run_hook, payloads, mock_server, project_dir, tmp_path
):
    subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:Org/SharedRepo.git"],
        cwd=project_dir,
        check=True,
    )
    other = tmp_path / "other"
    other.mkdir()
    env = {"COGNEE_DATASET_SCOPE": "project"}
    host_id = "project-scope-host"

    started = run_hook(
        suite,
        "session-start.py",
        stdin=payloads.session_start(session_id=host_id, cwd=str(project_dir)),
        cwd=project_dir,
        service_url=mock_server.url,
        env=env,
    )
    assert started.returncode == 0, started.stderr

    recalled = run_hook(
        suite,
        "session-context-lookup.py",
        stdin=payloads.user_prompt(
            session_id=host_id,
            cwd=str(other),
            prompt="recall project decision",
        ),
        cwd=other,
        service_url=mock_server.url,
        env=env,
    )
    assert recalled.returncode == 0, recalled.stderr

    calls = [call for call in mock_server.calls if call["path"] == "/api/v1/recall"]
    datasets = {tuple(call["json"].get("datasets") or []) for call in calls}
    assert len(datasets) == 1
    only = next(iter(datasets))
    assert len(only) == 1 and only[0].startswith("project_sharedrepo_")

    prompted = run_hook(
        suite,
        "store-user-prompt.py",
        stdin=payloads.user_prompt(
            session_id=host_id,
            cwd=str(other),
            prompt="remember the pinned project decision",
        ),
        cwd=other,
        service_url=mock_server.url,
        env=env,
    )
    assert prompted.returncode == 0, prompted.stderr

    stopped = run_hook(
        suite,
        "store-to-session.py",
        "--stop",
        stdin=payloads.stop(
            session_id=host_id,
            assistant_message="the pinned project decision is stable",
            cwd=str(other),
        ),
        cwd=other,
        service_url=mock_server.url,
        env=env,
    )
    assert stopped.returncode == 0, stopped.stderr
    writes = [call for call in mock_server.calls if call["path"] == "/api/v1/remember/entry"]
    assert writes
    assert {call["json"]["dataset_name"] for call in writes} == {only[0]}

    synced = run_hook(
        suite,
        "sync-session-to-graph.py",
        stdin={"hook_event_name": "ManualSync", "session_id": host_id, "cwd": str(other)},
        cwd=other,
        service_url=mock_server.url,
        env=env,
    )
    assert synced.returncode == 0, synced.stderr
    improves = [call for call in mock_server.calls if call["path"] == "/api/v1/improve"]
    assert improves
    assert improves[-1]["json"]["dataset_name"] == only[0]
