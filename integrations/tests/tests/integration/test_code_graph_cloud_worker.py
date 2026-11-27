"""On Cognee Cloud, the session's own code-graph settings decide its startup pass.

The startup code-graph pass of a Cloud session runs in the cloud worker: one
long-lived process shared by every terminal, which outlives edits of
~/.cognee/.env. Its own environment holds whatever COGNEE_CODE_AUTOINDEX,
COGNEE_CODE_SERVER_SHARES_FILES and COGNEE_CODING_MEMORY were when it started.
These tests run the real chain: the SessionStart hook as its own process writes
the session's record, and the worker's bootstrap loop runs in another process
with a stale environment. Only HTTP is a mock, reached as 127.1 so that it reads
as a remote server.

Pinned here:
  * the settings the session's SessionStart read decide, and the worker's own
    environment is left as it was;
  * removing ``always`` takes effect from the next session; a value the plugin
    does not know means ``ask``; a record from an older build indexes nothing new;
  * a pass deferred by failed registrations indexes nothing new, since the
    standing yes it was started with may have been withdrawn since;
  * a session in coding memory mode is not indexed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import pytest
from utils.isolation import build_env

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="reaches the mock server through the 127.1 shorthand"
)

CLONE_URL = "https://github.com/acme/proj"
SETTINGS = ("COGNEE_CODE_AUTOINDEX", "COGNEE_CODE_SERVER_SHARES_FILES", "COGNEE_CODING_MEMORY")

# The worker's bootstrap loop, as run() calls it: _plugin_common loads
# ~/.cognee/.env first (without replacing what the environment already holds),
# and target() refreshes it on every pass.
WORKER = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
import _cloud_worker as worker
import _plugin_common
worker.target()
names = json.loads(sys.argv[2])
before = {name: os.environ.get(name) for name in names}
worker._bootstrap_sessions()
after = {name: os.environ.get(name) for name in names}
print("ENV " + json.dumps({"before": before, "after": after}))
"""


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def claude(suite):
    if suite.name != "claude-code":
        pytest.skip("only Claude Code has the cloud worker")
    return suite


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "app.py").write_text("def start():\n    return 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "remote", "add", "origin", "git@github.com:acme/proj.git")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


@pytest.fixture
def cloud(private_mock_server):
    """The mock server under a name that is not a loopback name: a remote one."""
    private_mock_server.remote_url = private_mock_server.url.replace("127.0.0.1", "127.1")
    return private_mock_server


def _write_env_file(home, url, **settings):
    path = home / ".cognee" / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'COGNEE_BASE_URL="{url}"', 'COGNEE_API_KEY="test-api-key"']
    lines += [f'{name}="{value}"' for name, value in settings.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _env(suite, home, repo, url, extra=None):
    return build_env(suite, home, cwd=repo, extra={"COGNEE_PLATFORM_API_URL": url, **(extra or {})})


def _session_start(suite, home, repo, url, session_id="session-a", extra=None):
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": session_id,
        "cwd": str(repo),
        "source": "startup",
    }
    proc = subprocess.run(
        [sys.executable, str(suite.scripts_dir / "session-start.py")],
        input=json.dumps(payload),
        env=_env(suite, home, repo, url, extra),
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc


def _run_worker(suite, home, repo, url, worker_env=None) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", WORKER, str(suite.scripts_dir), json.dumps(SETTINGS)],
        env=_env(suite, home, repo, url, worker_env),
        cwd=str(home),
        capture_output=True,
        text=True,
        timeout=180,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("ENV ")]
    assert lines, proc.stderr[-2000:]
    return json.loads(lines[-1][len("ENV ") :])


def _record_path(home, session_id="session-a"):
    return home / ".cognee-plugin" / "claude-code" / "sessions" / session_id / "worker.json"


def _edit_record(home, session_id="session-a", **changes):
    path = _record_path(home, session_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    for name, value in changes.items():
        if value is None:
            record.pop(name, None)
        else:
            record[name] = value
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def _code_submissions(server) -> list:
    return [
        call["form"]["raw_data"]
        for call in server.calls
        if call["method"] == "POST"
        and call["path"] == "/api/v1/remember"
        and (call.get("form") or {}).get("content_type") == "code"
    ]


def _startup_outcomes(home) -> list:
    log = home / ".cognee-plugin" / "claude-code" / "hook.log"
    outcomes = []
    for line in log.read_text(encoding="utf-8").splitlines() if log.exists() else []:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("event") == "code_autoindex":
            outcomes.append(entry.get("detail") or {})
    return outcomes


# ── the session's own settings decide ──────────────────────────────────────


def test_removing_always_takes_effect_from_the_next_session(claude, temp_home, repo, cloud):
    """The worker started while ``always`` was set; the user has removed it."""
    _write_env_file(temp_home, cloud.remote_url)
    _session_start(claude, temp_home, repo, cloud.remote_url)
    record = json.loads(_record_path(temp_home).read_text(encoding="utf-8"))
    assert record["code_settings"] == dict.fromkeys(SETTINGS, "")

    env = _run_worker(
        claude, temp_home, repo, cloud.remote_url, {"COGNEE_CODE_AUTOINDEX": "always"}
    )
    assert _code_submissions(cloud) == []
    assert _startup_outcomes(temp_home)[-1]["skipped"] == "awaiting_consent"
    assert env["after"] == env["before"]
    assert env["after"]["COGNEE_CODE_AUTOINDEX"] == "always"


def test_a_session_with_always_is_indexed_by_its_clone_url(claude, temp_home, repo, cloud):
    _write_env_file(temp_home, cloud.remote_url, COGNEE_CODE_AUTOINDEX="always")
    _session_start(claude, temp_home, repo, cloud.remote_url)
    env = _run_worker(claude, temp_home, repo, cloud.remote_url)
    assert _code_submissions(cloud) == [CLONE_URL]
    assert env["after"] == env["before"]


def test_a_value_the_plugin_does_not_know_means_ask(claude, temp_home, repo, cloud):
    _write_env_file(temp_home, cloud.remote_url, COGNEE_CODE_AUTOINDEX="sometimes")
    _session_start(claude, temp_home, repo, cloud.remote_url)
    _run_worker(claude, temp_home, repo, cloud.remote_url, {"COGNEE_CODE_AUTOINDEX": "always"})
    assert _code_submissions(cloud) == []
    assert _startup_outcomes(temp_home)[-1]["skipped"] == "awaiting_consent"


def test_off_exported_in_the_sessions_terminal_wins(claude, temp_home, repo, cloud):
    _write_env_file(temp_home, cloud.remote_url, COGNEE_CODE_AUTOINDEX="always")
    _session_start(
        claude, temp_home, repo, cloud.remote_url, extra={"COGNEE_CODE_AUTOINDEX": "off"}
    )
    _run_worker(claude, temp_home, repo, cloud.remote_url)
    assert _code_submissions(cloud) == []
    assert _startup_outcomes(temp_home)[-1]["skipped"] == "autoindex_off"


def test_the_sessions_shares_files_setting_decides_how_the_repo_is_read(
    claude, temp_home, repo, cloud
):
    """The session declares a server that reads this disk; the worker started
    without that (an exported ``false`` the file cannot replace)."""
    _write_env_file(
        temp_home,
        cloud.remote_url,
        COGNEE_CODE_AUTOINDEX="always",
        COGNEE_CODE_SERVER_SHARES_FILES="true",
    )
    _session_start(claude, temp_home, repo, cloud.remote_url)
    env = _run_worker(
        claude,
        temp_home,
        repo,
        cloud.remote_url,
        {"COGNEE_CODE_SERVER_SHARES_FILES": "false"},
    )
    assert _code_submissions(cloud) == [str(repo.resolve())]
    assert env["after"]["COGNEE_CODE_SERVER_SHARES_FILES"] == "false"


def test_a_stale_shares_files_setting_in_the_worker_is_not_used(claude, temp_home, repo, cloud):
    _write_env_file(temp_home, cloud.remote_url, COGNEE_CODE_AUTOINDEX="always")
    _session_start(claude, temp_home, repo, cloud.remote_url)
    env = _run_worker(
        claude, temp_home, repo, cloud.remote_url, {"COGNEE_CODE_SERVER_SHARES_FILES": "true"}
    )
    assert _code_submissions(cloud) == [CLONE_URL]
    assert env["after"]["COGNEE_CODE_SERVER_SHARES_FILES"] == "true"


def test_a_coding_memory_session_is_not_indexed(claude, temp_home, repo, cloud):
    """Coding memory leaves the code graph alone. The session's terminal turns
    it on; the shared worker was started from another terminal."""
    _write_env_file(temp_home, cloud.remote_url, COGNEE_CODE_AUTOINDEX="always")
    _session_start(
        claude, temp_home, repo, cloud.remote_url, extra={"COGNEE_CODING_MEMORY": "true"}
    )
    _run_worker(claude, temp_home, repo, cloud.remote_url)
    assert _code_submissions(cloud) == []


def test_a_record_from_an_older_build_indexes_nothing_new(claude, temp_home, repo, cloud):
    _write_env_file(temp_home, cloud.remote_url)
    _session_start(claude, temp_home, repo, cloud.remote_url)
    _edit_record(temp_home, code_settings=None)
    _run_worker(claude, temp_home, repo, cloud.remote_url, {"COGNEE_CODE_AUTOINDEX": "always"})
    assert _code_submissions(cloud) == []


# ── a pass deferred by failed registrations ────────────────────────────────


def test_a_deferred_pass_indexes_nothing_new(claude, temp_home, repo, cloud):
    """Session A starts with ``always`` while Cloud refuses its registration.
    The user then removes ``always`` (session B is asked). When Cloud is back,
    A's deferred pass must not index on the yes it was started with."""
    _write_env_file(temp_home, cloud.remote_url, COGNEE_CODE_AUTOINDEX="always")
    _session_start(claude, temp_home, repo, cloud.remote_url, session_id="session-a")
    cloud.force_response("POST", "/api/v1/agents/register", 503, {"detail": "down"})
    _run_worker(claude, temp_home, repo, cloud.remote_url)
    record = json.loads(_record_path(temp_home).read_text(encoding="utf-8"))
    assert record.get("registered") is not True and record.get("next_attempt")

    _write_env_file(temp_home, cloud.remote_url)
    hook_b = _session_start(claude, temp_home, repo, cloud.remote_url, session_id="session-b")
    assert "Index this repository in Cognee's code graph?" in hook_b.stdout

    cloud.clear_forced()
    _edit_record(temp_home, next_attempt=0)
    _run_worker(claude, temp_home, repo, cloud.remote_url)
    assert _code_submissions(cloud) == []


def test_a_never_holds_against_a_deferred_always(claude, temp_home, repo, cloud):
    if not shutil.which("bash"):
        pytest.skip("needs bash")
    _write_env_file(temp_home, cloud.remote_url, COGNEE_CODE_AUTOINDEX="always")
    _session_start(claude, temp_home, repo, cloud.remote_url, session_id="session-a")
    cloud.force_response("POST", "/api/v1/agents/register", 503, {"detail": "down"})
    _run_worker(claude, temp_home, repo, cloud.remote_url)

    _write_env_file(temp_home, cloud.remote_url)
    _session_start(claude, temp_home, repo, cloud.remote_url, session_id="session-b")
    declined = subprocess.run(
        [
            "bash",
            str(claude.scripts_dir / "cognee-index-repo.sh"),
            str(repo),
            "--server",
            cloud.remote_url,
            "--expect-server",
            "remote",
            "--decline",
        ],
        env=_env(claude, temp_home, repo, cloud.remote_url),
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert '"declined"' in declined.stdout, declined.stdout + declined.stderr

    cloud.clear_forced()
    _edit_record(temp_home, session_id="session-a", next_attempt=0)
    _run_worker(claude, temp_home, repo, cloud.remote_url)
    assert _code_submissions(cloud) == []


def test_a_days_old_unregistered_session_indexes_nothing_new(claude, temp_home, repo, cloud):
    _write_env_file(temp_home, cloud.remote_url, COGNEE_CODE_AUTOINDEX="always")
    _session_start(claude, temp_home, repo, cloud.remote_url)
    three_days_ago = time.time() - 3 * 86400
    _edit_record(
        temp_home,
        created_at=three_days_ago,
        attempts=9,
        next_attempt=three_days_ago + 3600,
    )
    _write_env_file(temp_home, cloud.remote_url)
    _run_worker(claude, temp_home, repo, cloud.remote_url)
    assert _code_submissions(cloud) == []
