"""Session-start auto-indexing: what gets indexed without being asked.

Opening a coding agent in a repo should give you a code graph without a setup
step. But "index whatever directory the user happened to open" is also how a
plugin ends up shipping a private checkout to a hosted tenant, or burning
minutes of CPU on a monorepo nobody asked about. The policy these tests pin:

  * a LOCAL server auto-indexes freely — the code never leaves the machine,
    the server reads the working tree in place;
  * a REMOTE server does not, unless explicitly opted in, because there
    indexing means sending code somewhere;
  * an already-indexed repo is always refreshed regardless of that setting —
    the index command was the consent, and a stale graph is the failure the
    freshness loop exists to prevent;
  * a directory that is not a git repo, holds no code, or is enormous is left
    alone.
"""

from __future__ import annotations

import subprocess

import pytest

REMEMBER = "/api/v1/remember"


@pytest.fixture
def cg(suite, isolated_modules):
    return isolated_modules(suite, "_code_graph")


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    (repo / "app.py").write_text("def start():\n    return 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    return repo


# ── the local-server default ───────────────────────────────────────────────


def test_local_server_indexes_a_new_repo(cg, mock_server, git_repo):
    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=True
    )
    assert outcome["indexed"] is True
    assert outcome["reason"] == "first_seen"
    assert mock_server.assert_called("POST", REMEMBER)["form"]["content_type"] == "code"
    # The state file is what later turns key their freshness off.
    assert cg.load_repo_states()[0]["repo_root"] == str(git_repo.resolve())


def test_indexing_works_from_a_subdirectory(cg, mock_server, git_repo):
    """Agents are opened deep inside repos as often as at the root."""
    sub = git_repo / "src" / "deep"
    sub.mkdir(parents=True)
    outcome = cg.autoindex_on_session_start(str(sub), mock_server.url, "", is_local_server=True)
    assert outcome["indexed"] is True
    assert outcome["repo_root"] == str(git_repo.resolve())


# ── the remote-server guard ────────────────────────────────────────────────


def test_remote_server_does_not_index_a_new_repo(cg, mock_server, git_repo):
    """Auto-shipping a private checkout to a hosted tenant is not a decision a
    plugin gets to make silently."""
    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=False
    )
    assert outcome["skipped"] == "remote_server"
    mock_server.assert_not_called("POST", REMEMBER)


def test_remote_server_indexes_when_explicitly_opted_in(cg, mock_server, git_repo, monkeypatch):
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", "always")
    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=False
    )
    assert outcome["indexed"] is True


def test_autoindex_can_be_turned_off(cg, mock_server, git_repo, monkeypatch):
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", "off")
    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=True
    )
    assert outcome["skipped"] == "autoindex_off"
    mock_server.assert_not_called("POST", REMEMBER)


@pytest.mark.parametrize(
    "value,expected",
    [("", "auto"), ("off", "off"), ("always", "always"), ("nonsense", "auto")],
)
def test_autoindex_mode_parsing(cg, monkeypatch, value, expected):
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", value)
    assert cg.autoindex_mode() == expected


# ── refreshing an already-indexed repo ─────────────────────────────────────


def test_indexed_repo_is_refreshed_after_outside_edits(cg, mock_server, git_repo):
    """Edits made while the agent was away are exactly what a session-start
    refresh is for."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    mock_server.calls.clear()
    (git_repo / "app.py").write_text("def start():\n    return 2\n", encoding="utf-8")

    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=True
    )
    assert outcome["reason"] == "refresh"
    assert outcome["submitted"] is True
    mock_server.assert_called("POST", REMEMBER)


def test_unchanged_indexed_repo_submits_nothing(cg, mock_server, git_repo):
    cg.index_repo(mock_server.url, "", str(git_repo))
    mock_server.calls.clear()

    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=True
    )
    assert outcome["changed"] is False
    mock_server.assert_not_called("POST", REMEMBER)


def test_refresh_ignores_the_autoindex_off_switch(cg, mock_server, git_repo, monkeypatch):
    """The setting governs indexing repos nobody asked for — not keeping an
    explicitly indexed one honest."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    mock_server.calls.clear()
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", "off")
    (git_repo / "app.py").write_text("def start():\n    return 3\n", encoding="utf-8")

    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=True
    )
    assert outcome["submitted"] is True


def test_remote_indexed_repo_is_still_refreshed(cg, mock_server, git_repo, monkeypatch):
    """A repo indexed against a cloud server keeps working: the remote guard
    only gates NEW repos."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    mock_server.calls.clear()
    (git_repo / "app.py").write_text("def start():\n    return 4\n", encoding="utf-8")

    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=False
    )
    assert outcome["submitted"] is True


def test_session_start_retries_despite_an_active_backoff(cg, mock_server, git_repo):
    """Restarting the agent is the gesture a user makes after fixing what broke
    indexing (a corrected key, a restarted server), so a session start gets an
    attempt instead of waiting out a window earned by the previous session.
    Bounded by launches rather than turns, which is what the per-turn backoff
    exists to limit."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    (git_repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    mock_server.force_response("POST", REMEMBER, 401, {"detail": "nope"})
    cg.reingest_if_changed(str(git_repo), mock_server.url, "")
    assert cg.load_repo_states()[0]["error_count"] == 1

    # The user fixes the credential and relaunches; no window has elapsed.
    mock_server.clear_forced()
    mock_server.calls.clear()
    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=True
    )
    assert outcome["submitted"] is True
    assert "error_count" not in cg.load_repo_states()[0]


# ── directories that must be left alone ────────────────────────────────────


def test_non_git_directory_is_ignored(cg, mock_server, tmp_path):
    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert cg.autoindex_on_session_start(str(plain), mock_server.url, "") == {}
    mock_server.assert_not_called("POST", REMEMBER)


def test_repo_without_code_is_skipped(cg, mock_server, tmp_path):
    """A docs or config repo has nothing for enola to extract."""
    repo = tmp_path / "docs"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# hi\n", encoding="utf-8")

    outcome = cg.autoindex_on_session_start(str(repo), mock_server.url, "", is_local_server=True)
    assert outcome["skipped"] == "no_code_files"
    mock_server.assert_not_called("POST", REMEMBER)


def test_huge_repo_is_skipped(cg, mock_server, git_repo, monkeypatch):
    """The enola pass covers the whole tree; spending that on a monorepo
    nobody asked about is worse than doing nothing. Explicit indexing has no cap."""
    monkeypatch.setattr(cg, "_AUTOINDEX_MAX_FILES", 2)
    for i in range(5):
        (git_repo / f"mod{i}.py").write_text("x = 1\n", encoding="utf-8")

    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=True
    )
    assert outcome["skipped"] == "too_large"
    mock_server.assert_not_called("POST", REMEMBER)


def test_vendored_directories_do_not_count_toward_the_cap(cg, git_repo):
    """node_modules/.venv would blow the cap on an otherwise small repo."""
    vendor = git_repo / "node_modules" / "pkg"
    vendor.mkdir(parents=True)
    for i in range(50):
        (vendor / f"v{i}.js").write_text("x=1\n", encoding="utf-8")
    assert cg._source_file_count(str(git_repo), 100) == 1


def test_failures_never_escape(cg, git_repo):
    """Session start must survive an unreachable server."""
    outcome = cg.autoindex_on_session_start(
        str(git_repo), "http://127.0.0.1:1", "", is_local_server=True
    )
    assert outcome["indexed"] is False
    assert outcome["error"] == "unreachable"
