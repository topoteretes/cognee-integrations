"""The automatic code review: its gate, the detached run and the once-per-commit rule.

``auto-review.py`` is an asynchronous PostToolUse hook for Bash. The gate
(``_review_gate``) reads the call and local state only: it asks for a review
when a Bash call that did not fail ran ``git commit``, ``git push`` or
``gh pr create`` in a repository indexed by path, the change holds code files,
a base exists and this HEAD was not reviewed against it yet. Everything else is
one ``auto_review_skipped`` line and no request.

Claude Code only: the other hosts do not ship the review.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from utils.code_analysis import ANALYZE, RECALL, FakeServer, code_rows, index_by_path, make_repo

HEREDOC_COMMIT = """git add -A && git commit -m "$(cat <<'EOF'
Fix the parser, don't break (the lexer

Co-Authored-By: Someone <x@example.com>
EOF
)\""""
HEREDOC_PR = """gh pr create --title "t" --body "$(cat <<'EOF'
body (with a paren and a " quote
EOF
)\""""
HEREDOC_BODY = """cat > notes.md <<EOF
run git commit -m x, then git push
EOF"""
QUOTED_HEREDOC_BODY = """python3 - <<'PY'
print("gh pr create")
PY
ls"""

TRIGGERS = [
    ("git commit -m x", [("commit", "")]),
    ("git commit --amend --no-edit", [("commit", "")]),
    ('git commit -m "a; b && git push" ; ls', [("commit", "")]),
    ("cd sub && git commit -m 'x'", [("commit", "sub")]),
    ("cd a; cd b; git commit -m x", [("commit", "a/b")]),
    ("(cd sub && git commit -m y)", [("commit", "sub")]),
    ("git -C path commit -m x", [("commit", "path")]),
    ("cd a && git -C b push", [("push", "a/b")]),
    ("git -c user.name=x --no-pager commit -m y", [("commit", "")]),
    ("GIT_AUTHOR_NAME=x git commit -m y", [("commit", "")]),
    ("env FOO=1 git commit -m y", [("commit", "")]),
    ("/usr/bin/git commit -m y", [("commit", "")]),
    ("rtk git commit -m y", [("commit", "")]),
    ("rtk proxy git push", [("push", "")]),
    ("git add .\ngit commit -m x", [("commit", "")]),
    ("git commit -m x 2>&1 | tail -3", [("commit", "")]),
    ("if git commit -m x; then echo ok; fi", [("commit", "")]),
    (HEREDOC_COMMIT, [("commit", "")]),
    ("git push", [("push", "")]),
    ("git push -u origin feature/x", [("push", "")]),
    ("git commit -m x && git push", [("commit", ""), ("push", "")]),
    ("gh pr create --title x --body y", [("pr_create", "")]),
    ("gh -R owner/repo pr create --fill", [("pr_create", "")]),
    (HEREDOC_PR, [("pr_create", "")]),
]
NOT_TRIGGERS = [
    "ls -la",
    "",
    "git status",
    "git stash",
    "git stash push -m 'git commit'",
    "git commit --dry-run",
    "git commit -m x --dry-run",
    "git commit --help",
    "git push --dry-run",
    "git push -n origin main",
    "git push origin --delete old-branch",
    "git commit-tree abc",
    "git log --grep='git commit'",
    "git config alias.ci commit",
    "git show HEAD:commit",
    'echo "git commit -m x"',
    "echo 'git push'",
    "echo git push",
    "grep -rn 'gh pr create' docs",
    "# git commit -m x",
    "ls # git push",
    'bash -c "git commit -m x"',
    "echo $(git commit -m x)",
    "echo `git push`",
    HEREDOC_BODY,
    QUOTED_HEREDOC_BODY,
    "gh pr view 12",
    "gh pr merge 12 --squash",
    "gh pr list",
    "gh pr create --help",
    "gh pr create --dry-run --fill",
    "gh issue create --title 'pr create'",
]


@pytest.fixture
def gate(suite, isolated_modules, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("the automatic review ships with the Claude Code plugin")
    module = isolated_modules(suite, "_review_gate")
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    monkeypatch.delenv("COGNEE_AUTO_REVIEW", raising=False)
    return module


@pytest.mark.parametrize(
    ("command", "expected"), TRIGGERS, ids=[" ".join(c.split())[:40] for c, _ in TRIGGERS]
)
def test_commands_that_trigger(gate, command, expected):
    assert [(t["kind"], t["dir"]) for t in gate.triggers(command)] == expected


@pytest.mark.parametrize(
    "command", NOT_TRIGGERS, ids=[" ".join(c.split())[:40] or "empty" for c in NOT_TRIGGERS]
)
def test_commands_that_do_not_trigger(gate, command):
    assert gate.triggers(command) == []


# -- The conditions ------------------------------------------------------------------


def git(repo, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo, path: str, text: str, message: str = "change") -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def ahead_of_origin(root):
    """A checkout one code commit ahead of its remote default branch."""
    make_repo(root)
    git(root, "update-ref", "refs/remotes/origin/main", git(root, "rev-parse", "HEAD"))
    git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    commit(root, "pricing.py", "def line_total(price, qty):\n    return round(price * qty, 2)\n")
    return root


@pytest.fixture
def repo(gate, tmp_path):
    root = ahead_of_origin(tmp_path / "proj")
    index_by_path(gate._code_graph, root)
    return root


def call(repo, command="git commit -m x", **fields) -> dict:
    return {
        "session_id": "s1",
        "cwd": str(repo),
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": "ok", "stderr": "", "interrupted": False},
        **fields,
    }


def test_a_commit_of_code_in_an_indexed_repository_is_reviewed(gate, repo):
    assert gate.decide(call(repo)) == {
        "run": True,
        "trigger": "commit",
        "repo_root": os.path.realpath(str(repo)),
        "base": "origin/main",
        "base_commit": git(repo, "rev-parse", "origin/main"),
        "head": git(repo, "rev-parse", "HEAD"),
    }


def test_the_setting_and_the_mode_turn_it_off(gate, repo, monkeypatch):
    for value in ("false", "0", "off", "NO"):
        monkeypatch.setenv("COGNEE_AUTO_REVIEW", value)
        assert gate.decide(call(repo))["reason"] == "disabled"
    monkeypatch.setenv("COGNEE_AUTO_REVIEW", "true")
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "false")
    assert gate.decide(call(repo))["reason"] == "coding_memory_off"


def test_only_a_bash_call_that_did_not_fail_counts(gate, repo):
    assert gate.decide(call(repo, tool_name="Edit"))["reason"] == "not_bash"
    assert gate.decide(call(repo, "ls"))["reason"] == "not_a_trigger"
    for response in ({"interrupted": True}, {"is_error": True}, {"exit_code": 1}):
        assert gate.decide(call(repo, tool_response=response))["reason"] == "call_failed"
    assert gate.decide(call(repo, tool_response="committed"))["run"] is True


def test_the_repository_must_be_git_and_indexed_by_path(gate, repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gate.decide(call(plain))["reason"] == "not_in_git"
    other = make_repo(tmp_path / "other")
    assert gate.decide(call(other))["reason"] == "not_indexed_by_path"
    by_url = make_repo(tmp_path / "by-url")
    gate._code_graph.save_repo_state(
        {"spec": "https://github.com/x/by-url", "spec_kind": "url", "dataset": "d"}
    )
    assert gate.decide(call(by_url))["reason"] == "not_indexed_by_path"


def test_the_directory_of_the_command_decides_the_repository(gate, repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert gate.decide(call(elsewhere))["reason"] == "not_in_git"
    assert gate.decide(call(elsewhere, f"cd {repo} && git commit -m x"))["run"] is True
    gate.settle(str(repo), status="failed")
    assert gate.decide(call(elsewhere, f"git -C {repo} commit -m x"))["run"] is True


def test_a_server_known_to_lack_the_route_is_skipped_without_a_request(gate, repo):
    url = "http://127.0.0.1:1"
    gate._code_analysis._store_capability(url, "analyze", False)
    assert gate.decide(call(repo), service_url=lambda: url)["reason"] == "server_unsupported"
    # Unknown or supported: the review itself asks.
    assert gate.decide(call(repo), service_url=lambda: "http://127.0.0.1:2")["run"] is True


def test_the_base_is_the_remote_default_branch_or_the_upstream(gate, repo):
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert gate.base_ref(str(repo)) == ""
    assert gate.decide(call(repo))["reason"] == "no_base"
    git(repo, "branch", "trunk", "HEAD~1")
    git(repo, "config", "branch.main.remote", ".")
    git(repo, "config", "branch.main.merge", "refs/heads/trunk")
    assert gate.base_ref(str(repo)) == "trunk"
    assert gate.decide(call(repo))["base"] == "trunk"


def test_a_commit_without_code_files_is_not_reviewed(gate, repo):
    commit(repo, "README.md", "docs\n")
    assert gate.decide(call(repo))["reason"] == "no_code_change"
    commit(repo, "uv.lock", "lock\n")
    assert gate.decide(call(repo))["reason"] == "no_code_change"
    # A push or a pull request publishes everything since the base: the code commit counts.
    assert gate.decide(call(repo, "git push"))["trigger"] == "push"


def test_a_push_with_nothing_ahead_of_the_base_is_not_reviewed(gate, repo):
    git(repo, "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD"))
    assert gate.decide(call(repo, "git push"))["reason"] == "no_code_change"


def test_a_commit_is_reviewed_once(gate, repo):
    assert gate.decide(call(repo))["run"] is True
    # Still running (this process holds the claim): not started twice.
    assert gate.decide(call(repo))["reason"] == "already_running"
    assert gate.decide(call(repo, "git push"))["reason"] == "already_running"
    gate.settle(str(repo), status="done")
    assert gate.decide(call(repo))["reason"] == "already_reviewed"
    assert gate.decide(call(repo, "git push"))["reason"] == "already_reviewed"
    # A new commit, an amended one included, is a new HEAD.
    commit(repo, "pricing.py", "def line_total(price, qty):\n    return price * qty\n")
    assert gate.decide(call(repo, "git commit --amend --no-edit"))["run"] is True


def test_a_review_that_died_or_failed_can_run_again(gate, repo):
    assert gate.decide(call(repo))["run"] is True
    gate.settle(str(repo), pid=2**22 + 12345)  # no such process
    assert gate.decide(call(repo))["run"] is True
    gate.settle(str(repo), status="failed")
    assert gate.decide(call(repo))["run"] is True


# -- The hook ------------------------------------------------------------------------


@pytest.fixture
def hook(gate, suite, hook_module, monkeypatch):
    module = hook_module(suite, "auto-review.py")
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    monkeypatch.setenv("COGNEE_BASE_URL", "http://127.0.0.1:1")
    events: list = []
    monkeypatch.setattr(module, "hook_log", lambda name, detail=None: events.append((name, detail)))
    spawned: list = []
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        """Record the detached review; every other process (git) runs."""
        if "--run" in argv:
            return spawned.append((argv, kwargs))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    return SimpleNamespace(module=module, events=events, spawned=spawned)


@pytest.fixture
def indexed(hook, tmp_path):
    """An indexed checkout ahead of its base, for the modules the hook just loaded."""
    import _code_graph

    root = ahead_of_origin(tmp_path / "proj")
    index_by_path(_code_graph, root)
    return root


def run_hook(hook, monkeypatch, payload) -> None:
    monkeypatch.setattr(sys, "argv", ["auto-review.py"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    hook.module.main()


def test_the_hook_spawns_one_detached_review(hook, indexed, monkeypatch):
    run_hook(hook, monkeypatch, call(indexed))
    ((argv, kwargs),) = hook.spawned
    assert argv[1:] == [
        os.path.abspath(hook.module.__file__),
        "--run",
        os.path.realpath(str(indexed)),
        "origin/main",
        git(indexed, "rev-parse", "HEAD"),
        "commit",
    ]
    # Detached: its own session, no pipe back to the hook.
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert [name for name, _ in hook.events] == ["auto_review_started"]
    # The same commit again (the push that follows it): nothing more.
    run_hook(hook, monkeypatch, call(indexed, "git push"))
    assert len(hook.spawned) == 1
    assert hook.events[-1] == (
        "auto_review_skipped",
        {"reason": "already_running", "trigger": "push"},
    )


@pytest.mark.parametrize("command", ["ls -la", "git commit --dry-run", "gh pr view 3"])
def test_anything_else_is_one_event_and_no_request(hook, indexed, monkeypatch, command):
    import _code_analysis

    def no_request(*args, **kwargs):
        pytest.fail("the gate made a request")

    monkeypatch.setattr(_code_analysis, "_HTTPS_OPENER", SimpleNamespace(open=no_request))
    run_hook(hook, monkeypatch, call(indexed, command))
    assert hook.spawned == []
    assert hook.events == [("auto_review_skipped", {"reason": "not_a_trigger", "trigger": ""})]


def test_a_docs_only_commit_is_one_event_and_nothing_more(hook, indexed, monkeypatch):
    commit(indexed, "README.md", "docs\n")
    run_hook(hook, monkeypatch, call(indexed))
    assert hook.spawned == []
    assert hook.events == [
        ("auto_review_skipped", {"reason": "no_code_change", "trigger": "commit"})
    ]


def test_the_hook_never_raises(hook, indexed, monkeypatch):
    for raw in ("", "not json", "[1, 2]"):
        monkeypatch.setattr(sys, "argv", ["auto-review.py"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        hook.module.main()
    assert hook.events == [] and hook.spawned == []

    def boom(*args, **kwargs):
        raise OSError("no fork")

    monkeypatch.setattr(hook.module, "_spawn_process", boom)
    run_hook(hook, monkeypatch, call(indexed))
    assert [name for name, _ in hook.events] == ["auto_review_spawn_failed"]
    # The claim is given back: the next trigger for this commit tries again.
    import _review_gate

    assert _review_gate.decide(call(indexed))["run"] is True

    monkeypatch.setattr(_review_gate, "decide", boom)
    run_hook(hook, monkeypatch, call(indexed))
    assert hook.events[-1][0] == "auto_review_gate_failed"


# -- The detached review -------------------------------------------------------------

CLEAN = {
    "status": "clean",
    "edges_added": 1,
    "edges_removed": 0,
    "facts_added": 0,
    "facts_removed": 0,
    "facts_changed": 1,
    "diff": {"comparability": {"comparable": True}},
}
SYMBOL = {
    "id": "a" * 32,
    "name": "pricing.line_total",
    "symbol_kind": "function",
    "file": "pricing.py",
    "line": 1,
    "end_line": 2,
    "lines": 2,
}


def reply(analysis: str, output, exit_code: int = 0, review=None) -> dict:
    return {"analysis": analysis, "exit_code": exit_code, "output": output, "review": review}


@pytest.fixture
def server(hook, indexed, monkeypatch):
    """A fake server answering the review's analyses for ``indexed``."""
    import _code_analysis
    import _code_review

    fake = FakeServer()
    stored = {
        "repo": "proj",
        "base": "origin/main",
        "base_commit": git(indexed, "rev-parse", "origin/main"),
        "reviewed_at": "2026-11-27T10:00:00+00:00",
        "graph_snapshot_id": "sha256:aaa",
        "rule_files_changed": [],
    }
    fake.by_analysis = {
        "check": reply("check", CLEAN, review=stored),
        "locate": reply(
            "locate", {"diff": {"files": [{"path": "pricing.py", "symbols": [SYMBOL]}]}}
        ),
        "impact": reply(
            "impact", {"by_depth": {"1": [{"name": "pricing.total"}], "2": [{"name": "main.main"}]}}
        ),
        "touched": reply("touched", {"commits": [], "ranking": []}),
    }
    fake.on("POST", ANALYZE, lambda body: (200, fake.by_analysis[body["analysis"]]))
    read_back = {
        **stored,
        "exit_code": 0,
        "verdict": CLEAN,
        "current_graph_snapshot_id": "sha256:aaa",
    }
    fake.on("POST", RECALL, (200, code_rows({"operation": "review", "reviews": [read_back]})))
    monkeypatch.setattr(_code_analysis, "_HTTPS_OPENER", SimpleNamespace(open=fake))
    monkeypatch.setattr(_code_review, "_refresh_graph", lambda *a, **k: "")
    monkeypatch.setattr(hook.module, "_runtime", lambda: fake.rt)
    return fake


def run_review(hook, indexed, server) -> dict:
    return hook.module.review(
        os.path.realpath(str(indexed)),
        "origin/main",
        git(indexed, "rev-parse", "HEAD"),
        "commit",
        opener=server,
    )


def test_the_review_asks_the_server_one_analysis_after_another(hook, indexed, server):
    import _code_review
    import _review_gate

    assert _review_gate.decide(call(indexed))["run"] is True
    outcome = run_review(hook, indexed, server)
    assert [body["analysis"] for body in server.bodies(ANALYZE)] == [
        "check",
        "locate",
        "impact",
        "touched",
    ]
    assert outcome["ok"] and outcome["status"] == "clean" and outcome["places"] == 1
    state = _review_gate._code_analysis.read_json(_review_gate.state_path(str(indexed)))
    assert state["status"] == "done" and state["pid"] == os.getpid()
    assert _review_gate.decide(call(indexed))["reason"] == "already_reviewed"

    # The next prompt shows the places once, although the verdict is clean.
    text, mark_shown = _code_review.prompt_notice(str(indexed), lambda: server.rt)
    assert text.splitlines() == [
        "Cognee code review of proj against origin/main (stored verdict, reviewed "
        "2026-11-27T10:00:00): clean",
        "Full report: cognee-review.sh --stored (the cognee-architecture skill).",
        "Where to look:",
        "- pricing.line_total (pricing.py:1): 2 symbol(s) depend on it (1 directly)",
    ]
    mark_shown()
    assert _code_review.prompt_notice(str(indexed), lambda: server.rt) == ("", None)


def test_a_clean_review_with_nothing_to_look_at_shows_nothing(hook, indexed, server):
    import _code_review

    server.by_analysis["impact"] = reply("impact", {"by_depth": {}})
    outcome = run_review(hook, indexed, server)
    assert outcome["ok"] and outcome["places"] == 0
    assert _code_review.prompt_notice(str(indexed), lambda: server.rt) == ("", None)


def test_a_review_the_server_cannot_run_is_one_failed_outcome(hook, indexed, server):
    import _review_gate

    server.on("POST", ANALYZE, (500, {"detail": "enola crashed"}))
    assert _review_gate.decide(call(indexed))["run"] is True
    outcome = run_review(hook, indexed, server)
    assert outcome["ok"] is False and outcome["code"] == "http_500"
    # Not reviewed: the next trigger for this commit may try again.
    assert _review_gate.decide(call(indexed))["run"] is True


def test_the_detached_entry_point_logs_one_event(hook, indexed, monkeypatch):
    argv = ["auto-review.py", "--run", str(indexed), "origin/main", "abc", "commit"]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(hook.module, "review", lambda *a: {"ok": True, "places": 0})
    hook.module.main()
    assert hook.events == [("auto_review_finished", {"ok": True, "places": 0})]

    def boom(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(hook.module, "review", boom)
    hook.module.main()
    assert hook.events[-1] == ("auto_review_failed", {"error": "boom"})
