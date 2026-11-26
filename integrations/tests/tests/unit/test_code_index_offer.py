"""SessionStart asks before a repository is indexed.

Indexing the repository a session opened in is the user's decision, on a local
server and on Cognee Cloud alike. A hook cannot put a question, so SessionStart
hands the agent one instruction: ask once, and run the index command only on a
yes. These tests pin what that instruction says and when it is given; what
``index_offer`` decides is pinned in ``integration/test_code_autoindex.py``.
"""

from __future__ import annotations

import subprocess

import pytest

STARTUP = {"source": "startup"}
CLOUD = "https://tenant.cognee.example"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    (root / "app.py").write_text("def start():\n    return 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("remote", "add", "origin", "git@github.com:acme/proj.git")
    return root.resolve()


@pytest.fixture
def start(suite, hook_module, monkeypatch):
    if not suite.asks_before_code_index:
        pytest.skip("this host indexes a new repository without asking")
    module = hook_module(suite, "session-start.py")
    events: list = []
    monkeypatch.setattr(module, "hook_log", lambda e, d=None: events.append((e, d)))
    for name in ("COGNEE_CODE_AUTOINDEX", "COGNEE_BASE_URL", "COGNEE_LOCAL_API_URL"):
        monkeypatch.delenv(name, raising=False)
    module.events = events
    return module


def _context(output: dict) -> str:
    return output.get("hookSpecificOutput", {}).get("additionalContext", "")


def test_a_local_session_is_told_to_ask(start, repo):
    out = start._apply_code_index_offer({}, {**STARTUP, "cwd": str(repo)})
    text = _context(out)
    assert f"not indexed yet: {repo} (1 source file)" in text
    assert "ask them once" in text and "AskUserQuestion" in text
    assert "Index this repository in Cognee's code graph?" in text
    assert "the code stays on this machine" in text
    # One command per answer, spelled with the installed script's path.
    script = start.Path(start.__file__).resolve().parent / "cognee-index-repo.sh"
    assert f'`"{script}" {repo} --wait 60`' in text
    assert f'`"{script}" {repo} --decline`' in text
    assert "Never index it without a yes" in text
    # For the agent only: the question itself is what the user sees.
    assert "systemMessage" not in out
    assert start.events == [
        ("code_index_offered", {"repo_root": str(repo), "server": "local", "source_files": 1})
    ]


def test_a_cloud_session_says_what_leaves_the_machine(start, repo, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", CLOUD)
    text = _context(start._apply_code_index_offer({}, {**STARTUP, "cwd": str(repo)}))
    assert "(tenant.cognee.example) cannot read this folder" in text
    assert "it would clone https://github.com/acme/proj itself" in text
    assert "only commits pushed to the default branch are indexed" in text
    assert "no local file is uploaded" in text
    assert "a private repository fails" in text
    assert "the code stays on this machine" not in text


def test_the_question_leads_the_context(start, repo):
    """An oversized SessionStart output reaches the agent as its head only, so
    the one instruction that needs an answer goes first."""
    out = start._apply_code_index_offer(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "DIGEST"}},
        {**STARTUP, "cwd": str(repo)},
    )
    text = _context(out)
    assert text.startswith("Cognee code graph:") and text.endswith("\n\nDIGEST")


def test_a_path_with_a_space_stays_one_argument(start, tmp_path):
    root = tmp_path / "my proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    text = _context(start._apply_code_index_offer({}, {**STARTUP, "cwd": str(root)}))
    assert f"'{root.resolve()}' --wait 60`" in text


@pytest.mark.parametrize("source", ["resume", "compact"])
def test_not_asked_in_the_middle_of_a_conversation(start, repo, source):
    assert start._apply_code_index_offer({}, {"source": source, "cwd": str(repo)}) == {}
    assert start.events == []


def test_not_asked_after_a_never(start, repo):
    import _code_graph

    _code_graph.decline_repo(str(repo), is_local_server=True)
    assert start._apply_code_index_offer({}, {**STARTUP, "cwd": str(repo)}) == {}


@pytest.mark.parametrize("mode", ["off", "always"])
def test_not_asked_when_the_setting_already_answers(start, repo, monkeypatch, mode):
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", mode)
    assert start._apply_code_index_offer({}, {**STARTUP, "cwd": str(repo)}) == {}


def test_not_asked_outside_a_repository(start, tmp_path):
    assert start._apply_code_index_offer({"a": 1}, {**STARTUP, "cwd": str(tmp_path)}) == {"a": 1}


def test_a_failure_costs_the_question_not_the_startup(start, repo, monkeypatch):
    def boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(start, "load_config", boom)
    out = start._apply_code_index_offer({"a": 1}, {**STARTUP, "cwd": str(repo)})
    assert out == {"a": 1}
    assert start.events == [("code_index_offer_failed", {"error": "config unreadable"})]
