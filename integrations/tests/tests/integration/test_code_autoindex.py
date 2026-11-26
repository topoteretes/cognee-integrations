"""Session-start indexing: what gets indexed, and who decides.

Opening a coding agent in a repo should give you a code graph without a setup
step. But "index whatever directory the user happened to open" is also how a
plugin ends up shipping a private checkout to a hosted tenant, or burning
minutes of CPU on a monorepo nobody asked about. Two policies are pinned here,
split by ``Suite.asks_before_code_index``.

Where the user is asked first (Claude Code):

  * a NEW repo is never indexed from session start on its own: SessionStart
    has the agent ask, a yes runs the index command, a "never" is recorded per
    repo and per kind of server;
  * a REMOTE server cannot read the checkout, so it is sent the repository's
    git remote as an https URL and clones it: nothing but that URL leaves the
    machine, and such a graph follows pushes, not edits;
  * ``COGNEE_CODE_AUTOINDEX=always`` is the standing yes, ``off`` the standing no.

Elsewhere (Codex, Cursor, Antigravity):

  * a LOCAL server auto-indexes freely — the code never leaves the machine,
    the server reads the working tree in place;
  * a REMOTE server does not, unless explicitly opted in.

On every host, an already-indexed repo is always refreshed regardless of the
setting — its index was the consent, and a stale graph is the failure the
freshness loop exists to prevent — and a directory that is not a git repo, holds
no code, or is enormous is left alone.
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


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


CLONE_URL = "https://github.com/acme/proj"


@pytest.fixture
def pushed_repo(git_repo):
    """A checkout whose default branch is pushed to a remote, as a clone is."""
    _git(git_repo, "remote", "add", "origin", "git@github.com:acme/proj.git")
    _push(git_repo)
    return git_repo


def _push(repo):
    """What a push leaves behind locally: the remote-tracking ref at HEAD."""
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


@pytest.fixture
def unasked(suite, monkeypatch):
    """Index a new repo with no question, on every host.

    Hosts that ask first need the standing yes for it; the others do it by
    default. Tests of the indexing itself (what counts, what is too large, what
    a failure looks like) use this so one body covers both.
    """
    if suite.asks_before_code_index:
        monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", "always")


@pytest.fixture
def asking(suite):
    if not suite.asks_before_code_index:
        pytest.skip("this host indexes a new repository without asking")


@pytest.fixture
def not_asking(suite):
    if suite.asks_before_code_index:
        pytest.skip("this host asks before it indexes a new repository")


# ── indexing a new repo, once it is allowed ────────────────────────────────


def test_local_server_indexes_a_new_repo(cg, mock_server, git_repo, unasked):
    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=True
    )
    assert outcome["indexed"] is True
    assert outcome["reason"] == "first_seen"
    assert mock_server.assert_called("POST", REMEMBER)["form"]["content_type"] == "code"
    # The state file is what later turns key their freshness off.
    assert cg.load_repo_states()[0]["repo_root"] == str(git_repo.resolve())


def test_indexing_works_from_a_subdirectory(cg, mock_server, git_repo, unasked):
    """Agents are opened deep inside repos as often as at the root."""
    sub = git_repo / "src" / "deep"
    sub.mkdir(parents=True)
    outcome = cg.autoindex_on_session_start(str(sub), mock_server.url, "", is_local_server=True)
    assert outcome["indexed"] is True
    assert outcome["repo_root"] == str(git_repo.resolve())


# ── the remote-server guard ────────────────────────────────────────────────


def test_remote_server_does_not_index_a_new_repo(cg, mock_server, git_repo, suite):
    """Auto-shipping a private checkout to a hosted tenant is not a decision a
    plugin gets to make silently."""
    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=False
    )
    # Where a remote server clones, a checkout with no remote has nothing to send.
    expected = "no_git_remote" if suite.asks_before_code_index else "remote_server"
    assert outcome["skipped"] == expected
    mock_server.assert_not_called("POST", REMEMBER)


def test_remote_server_indexes_when_explicitly_opted_in(
    cg, mock_server, git_repo, monkeypatch, not_asking
):
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", "always")
    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=False
    )
    assert outcome["indexed"] is True


# ── the user decides (hosts that ask first) ────────────────────────────────


@pytest.mark.parametrize("is_local", [True, False])
def test_a_new_repo_waits_for_the_user(cg, mock_server, pushed_repo, asking, is_local):
    """No setting, no answer yet: nothing is submitted, on any kind of server."""
    outcome = cg.autoindex_on_session_start(
        str(pushed_repo), mock_server.url, "", is_local_server=is_local
    )
    assert outcome == {"skipped": "awaiting_consent", "repo_root": str(pushed_repo.resolve())}
    mock_server.assert_not_called("POST", REMEMBER)
    assert cg.load_repo_states() == []


def test_the_offer_describes_a_local_index(cg, git_repo, asking):
    (git_repo / "new.py").write_text("y = 2\n", encoding="utf-8")  # untracked counts locally
    (git_repo / "notes.md").write_text("# no code\n", encoding="utf-8")
    assert cg.index_offer(str(git_repo), is_local_server=True) == {
        "repo_root": str(git_repo.resolve()),
        "server": "local",
        "source_files": 2,
    }


def test_the_offer_for_a_remote_server_names_what_would_be_cloned(cg, pushed_repo, asking):
    (pushed_repo / "new.py").write_text("y = 2\n", encoding="utf-8")  # a clone never has it
    assert cg.index_offer(str(pushed_repo), is_local_server=False) == {
        "repo_root": str(pushed_repo.resolve()),
        "server": "remote",
        "clone_url": CLONE_URL,
        "source_files": 1,
    }


def test_no_offer_for_a_remote_server_without_a_git_remote(cg, git_repo, asking):
    """A hosted server could only clone it, and there is nothing to clone."""
    assert cg.index_offer(str(git_repo), is_local_server=False) == {}
    assert cg.index_offer(str(git_repo), is_local_server=True)  # a local server still can


def test_no_offer_outside_a_code_repository(cg, tmp_path, asking):
    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert cg.index_offer(str(plain), is_local_server=True) == {}

    docs = tmp_path / "docs"
    docs.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=docs, check=True, capture_output=True)
    (docs / "README.md").write_text("# hi\n", encoding="utf-8")
    assert cg.index_offer(str(docs), is_local_server=True) == {}


@pytest.mark.parametrize("mode", ["off", "always"])
def test_no_offer_when_the_setting_already_answers(cg, git_repo, monkeypatch, asking, mode):
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", mode)
    assert cg.index_offer(str(git_repo), is_local_server=True) == {}


def test_an_indexed_repo_is_not_offered_again(cg, mock_server, git_repo, asking):
    cg.index_repo(mock_server.url, "", str(git_repo))
    assert cg.index_offer(str(git_repo), is_local_server=True) == {}


def test_never_is_remembered_per_kind_of_server(cg, mock_server, pushed_repo, asking):
    """Refusing to send a repository to a hosted tenant says nothing about a
    local server, where the code stays on the machine."""
    sub = pushed_repo / "src"
    sub.mkdir()
    assert cg.decline_repo(str(sub), is_local_server=False) == {
        "ok": True,
        "declined": str(pushed_repo.resolve()),
        "server": "remote",
    }
    assert cg.index_offer(str(pushed_repo), is_local_server=False) == {}
    assert cg.index_offer(str(pushed_repo), is_local_server=True)["server"] == "local"

    outcome = cg.autoindex_on_session_start(
        str(pushed_repo), mock_server.url, "", is_local_server=False
    )
    assert outcome["skipped"] == "declined"
    mock_server.assert_not_called("POST", REMEMBER)


def test_indexing_a_declined_repo_lifts_the_never(cg, mock_server, git_repo, asking):
    """The index command is a later yes; a stale "never" must not outlive it
    and silence the question after the index is gone."""
    cg.decline_repo(str(git_repo), is_local_server=True)
    cg.index_repo(mock_server.url, "", str(git_repo))
    assert not cg.is_declined(str(git_repo.resolve()), True)


def test_the_decline_command_submits_nothing(cg, mock_server, git_repo, asking, capsys):
    cg.main([mock_server.url, "", "decline", str(git_repo)])
    assert '"declined"' in capsys.readouterr().out
    assert cg.is_declined(str(git_repo.resolve()), True)
    mock_server.assert_not_called("POST", REMEMBER)


# ── a remote server clones the repository (hosts that ask first) ───────────


def test_a_remote_server_is_sent_the_git_remote_not_the_path(cg, mock_server, pushed_repo, asking):
    """Cognee Cloud cannot read this disk. The index command given the checkout
    submits the repository's https URL, and the state keeps the checkout so the
    cwd still resolves to the graph."""
    sub = pushed_repo / "src"
    sub.mkdir()
    result = cg.index_repo(mock_server.url, "", str(sub), is_local_server=False)
    assert result["ok"] is True

    form = mock_server.assert_called("POST", REMEMBER)["form"]
    assert form["raw_data"] == CLONE_URL
    assert form["content_type"] == "code"
    assert str(pushed_repo) not in str(form)

    state = cg.load_repo_states()[0]
    assert (state["spec"], state["spec_kind"]) == (CLONE_URL, "url")
    assert state["repo_root"] == str(pushed_repo.resolve())  # a clone is the whole repository
    assert cg.find_indexed_repo(str(sub))["dataset"] == result["dataset"]
    assert cg.index_offer(str(pushed_repo), is_local_server=False) == {}


def test_a_remote_server_is_never_sent_a_path_it_cannot_read(cg, mock_server, git_repo, asking):
    result = cg.index_repo(mock_server.url, "", str(git_repo), is_local_server=False)
    assert "no git remote" in result["error"]
    mock_server.assert_not_called("POST", REMEMBER)
    assert cg.load_repo_states() == []


def test_the_standing_yes_indexes_on_a_remote_server(
    cg, mock_server, pushed_repo, monkeypatch, asking
):
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", "always")
    outcome = cg.autoindex_on_session_start(
        str(pushed_repo), mock_server.url, "", is_local_server=False
    )
    assert outcome["indexed"] is True
    assert mock_server.assert_called("POST", REMEMBER)["form"]["raw_data"] == CLONE_URL


def test_the_standing_yes_skips_a_repo_with_nothing_to_clone(
    cg, mock_server, git_repo, monkeypatch, asking
):
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", "always")
    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=False
    )
    assert outcome["skipped"] == "no_git_remote"
    mock_server.assert_not_called("POST", REMEMBER)


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:acme/proj.git", "https://github.com/acme/proj"),
        ("ssh://git@gitlab.com:2222/group/sub/proj.git", "https://gitlab.com/group/sub/proj"),
        ("https://github.com/acme/proj.git", "https://github.com/acme/proj"),
        ("https://github.com/acme/proj/", "https://github.com/acme/proj"),
        # A token saved in the local git config is never sent anywhere.
        ("https://x-access-token:ghp_secret@github.com/acme/proj.git", CLONE_URL),
        ("https://git.example.com:8443/acme/proj.git", "https://git.example.com:8443/acme/proj"),
        # Nothing a server elsewhere could reach.
        ("/srv/git/proj.git", ""),
        ("../proj", ""),
        ("file:///srv/git/proj.git", ""),
        ("git://github.com/acme/proj.git", ""),
        ("git@buildbox:acme/proj.git", ""),
        ("", ""),
    ],
)
def test_git_remote_as_a_cloneable_url(cg, asking, remote, expected):
    assert cg._https_clone_url(remote) == expected


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://localhost:8011", True),
        ("http://127.0.0.1:8011/", True),
        ("http://[::1]:8011", True),
        ("https://tenant.cognee.example", False),
        ("http://192.168.1.20:8011", False),
        ("", False),
    ],
)
def test_the_index_command_tells_a_local_server_from_a_remote_one(cg, asking, url, local):
    """The command has no hook to tell it: the service URL decides whether the
    server reads the path or has to clone."""
    assert cg.service_is_local(url) is local


def test_a_cloned_repo_follows_pushes_not_edits(cg, mock_server, pushed_repo, asking):
    """A clone holds pushed commits only. Editing, even committing, changes
    nothing the server could pull; the push does."""
    cg.index_repo(mock_server.url, "", str(pushed_repo), is_local_server=False)
    mock_server.calls.clear()

    (pushed_repo / "app.py").write_text("def start():\n    return 2\n", encoding="utf-8")
    _git(pushed_repo, "commit", "-q", "-am", "edit")
    outcome = cg.autoindex_on_session_start(
        str(pushed_repo), mock_server.url, "", is_local_server=False
    )
    assert outcome["changed"] is False
    mock_server.assert_not_called("POST", REMEMBER)

    _push(pushed_repo)
    outcome = cg.autoindex_on_session_start(
        str(pushed_repo), mock_server.url, "", is_local_server=False
    )
    assert (outcome["reason"], outcome["submitted"]) == ("refresh", True)
    assert mock_server.assert_called("POST", REMEMBER)["form"]["raw_data"] == CLONE_URL


def test_a_graph_built_on_the_other_kind_of_server_is_left_alone(
    cg, mock_server, pushed_repo, asking
):
    """Each graph lives where it was built. A checkout indexed locally is still
    unindexed on Cognee Cloud (so it is asked about there), and the local
    server's turn-end refresh must not push a cloud clone's URL at it."""
    cg.index_repo(mock_server.url, "", str(pushed_repo), is_local_server=True)
    mock_server.calls.clear()
    (pushed_repo / "app.py").write_text("x = 5\n", encoding="utf-8")

    assert cg.index_offer(str(pushed_repo), is_local_server=False)["server"] == "remote"
    outcome = cg.autoindex_on_session_start(
        str(pushed_repo), mock_server.url, "", is_local_server=False
    )
    assert outcome["skipped"] == "awaiting_consent"
    assert (
        cg.reingest_if_changed(str(pushed_repo), mock_server.url, "", is_local_server=False) == {}
    )
    mock_server.assert_not_called("POST", REMEMBER)


def test_autoindex_can_be_turned_off(cg, mock_server, git_repo, monkeypatch):
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", "off")
    outcome = cg.autoindex_on_session_start(
        str(git_repo), mock_server.url, "", is_local_server=True
    )
    assert outcome["skipped"] == "autoindex_off"
    mock_server.assert_not_called("POST", REMEMBER)


@pytest.mark.parametrize(
    "value,expected",
    [("", "default"), ("off", "off"), ("always", "always"), ("nonsense", "default")],
)
def test_autoindex_mode_parsing(cg, suite, monkeypatch, value, expected):
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", value)
    if expected == "default":
        expected = "ask" if suite.asks_before_code_index else "auto"
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


def test_remote_indexed_repo_is_still_refreshed(cg, mock_server, git_repo, monkeypatch, not_asking):
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


def test_repo_without_code_is_skipped(cg, mock_server, tmp_path, unasked):
    """A docs or config repo has nothing for enola to extract."""
    repo = tmp_path / "docs"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# hi\n", encoding="utf-8")

    outcome = cg.autoindex_on_session_start(str(repo), mock_server.url, "", is_local_server=True)
    assert outcome["skipped"] == "no_code_files"
    mock_server.assert_not_called("POST", REMEMBER)


def test_huge_repo_is_skipped(cg, mock_server, git_repo, monkeypatch, unasked):
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


def test_failures_never_escape(cg, git_repo, unasked):
    """Session start must survive an unreachable server."""
    outcome = cg.autoindex_on_session_start(
        str(git_repo), "http://127.0.0.1:1", "", is_local_server=True
    )
    assert outcome["indexed"] is False
    assert outcome["error"] == "unreachable"
