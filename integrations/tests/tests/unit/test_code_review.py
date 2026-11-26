"""Architecture review of a change: the script, its report and the prompt notice.

The server runs enola; the plugin posts to ``/api/v1/code/analyze`` and reads
the stored verdict with the ``review`` CODE operation. The server here is a
fake opener (``utils.code_analysis.FakeServer``); the verdicts are the shapes a
real server returned for a small repository (cognee 029d36a78, enola 0.5.4).

Claude Code only: the other hosts do not ship these scripts.
"""

from __future__ import annotations

import json
import os
import socket
import time
from types import SimpleNamespace

import pytest
from utils.code_analysis import (
    ANALYZE,
    OPENAPI,
    RECALL,
    SPEC_WITHOUT_ROUTE,
    FakeServer,
    code_rows,
    index_by_path,
    make_repo,
)

REVIEW = {
    "repo": "proj",
    "base": "main",
    "base_commit": "bf6d4f7586df41348837f2a238d4f07931301f3e",
    "reviewed_at": "2026-11-26T17:17:25.662805+00:00",
    "graph_snapshot_id": "sha256:aaa",
    "rule_files_changed": [],
}
CYCLE = {
    "title": "Cyclic dependency detected (2 modules)",
    "source": "cycles",
    "evidence": [{"fact": "billing", "detail": "module is part of the cycle"}],
}
DEAD = {
    "title": "Dead code candidate: function billing/invoice.unused_helper",
    "source": "dead-code",
    "evidence": [{"file": "billing/invoice.py", "symbol": "x", "line": 6}],
}
REGRESSION = {
    "status": "regression",
    "failures": [CYCLE],
    "measurements": [
        {
            "name": "spillover_packages",
            "label": "package(s) reached outside the declared scope",
            "count": 1,
        }
    ],
    "breaches": [{"measurement": {"name": "spillover_packages", "count": 1}, "fatal": True}],
    "edges_added": 10,
    "edges_removed": 2,
    "facts_added": 9,
    "facts_removed": 0,
    "facts_changed": 1,
    "edge_kinds_added": {"calls": 1, "imports": 5, "declares": 3, "instantiates": 1},
    "edge_kinds_removed": {"imports": 2},
    "diff": {"comparability": {"comparable": True}, "findings_new": [CYCLE, DEAD, DEAD]},
}
CLEAN = {
    "status": "clean",
    "edges_added": 3,
    "edges_removed": 0,
    "facts_added": 3,
    "facts_removed": 0,
    "facts_changed": 0,
    "edge_kinds_added": {"imports": 3},
    "diff": {"comparability": {"comparable": True}},
}


def check_reply(verdict: dict, exit_code: int = 0, **review) -> dict:
    return {
        "analysis": "check",
        "dataset_id": "10d70858-43cf-5f39-919b-5aa182eda938",
        "data_ids": ["d20a90fe-e1cd-5bf6-b1b2-06b6bfcb5895"],
        "enola_version": "0.5.4",
        "exit_code": exit_code,
        "output": verdict,
        "review": {**REVIEW, **review},
    }


def stored(verdict: dict, exit_code: int = 0, **fields) -> dict:
    review = {
        **REVIEW,
        "enola_version": "0.5.4",
        "exit_code": exit_code,
        "current_graph_snapshot_id": "sha256:aaa",
        "verdict": verdict,
        **fields,
    }
    return {"operation": "review", "reviews": [review]}


@pytest.fixture
def env(suite, isolated_modules, monkeypatch, tmp_path):
    """The review modules, a fake server and a checkout indexed by path."""
    if suite.name != "claude-code":
        pytest.skip("the review scripts ship with the Claude Code plugin")
    # One load: each isolated load re-imports the plugin's modules, so the
    # copies the review module holds are the ones to patch.
    cr = isolated_modules(suite, "_code_review")
    ca, cg = cr._code_analysis, cr._code_graph
    server = FakeServer()
    monkeypatch.setattr(ca, "_HTTPS_OPENER", SimpleNamespace(open=server))
    repo = make_repo(tmp_path / "proj")
    state = index_by_path(cg, repo)
    return SimpleNamespace(cg=cg, ca=ca, cr=cr, server=server, repo=repo, state=state, rt=server.rt)


def run(env, base="main", **params):
    return env.cr.run_review(str(env.repo), base, params, env.rt)


# -- Running a review ------------------------------------------------------------


def test_a_review_posts_check_for_the_checkouts_code_dataset(env):
    env.server.on("POST", ANALYZE, (200, check_reply(CLEAN)))
    answer = run(env, fail_on=["cycles"], max_spillover=0)
    assert answer["ok"]
    assert env.server.bodies(ANALYZE) == [
        {
            "analysis": "check",
            "params": {"base": "main", "fail_on": ["cycles"], "max_spillover": 0},
            "dataset": env.state["dataset"],
        }
    ]


def test_the_report_of_a_regression_names_findings_edges_and_scope(env):
    text = env.cr.report(REVIEW, REGRESSION, 1, "0.5.4")
    assert text.splitlines()[0] == (
        "Cognee review: proj against main (merge-base bf6d4f7586), enola 0.5.4"
    )
    assert "Status: REGRESSION" in text
    assert "  - [cycles] Cyclic dependency detected (2 modules)" in text
    assert "New findings by explainer: dead-code 2, cycles 1" in text
    assert "(billing/invoice.py:6)" in text
    assert (
        "Edges added: 10 (imports 5, declares 3, calls 1, instantiates 1); removed: 2 (imports 2)"
        in text
    )
    assert "Facts added: 9; removed: 0; changed: 1" in text
    assert (
        "Outside the expected scope: 1 package(s) reached outside the declared scope "
        "(over the limit)"
    ) in text
    assert "RULE FILES" not in text and "reloaded" not in text


def test_the_report_of_a_clean_review(env):
    text = env.cr.report(REVIEW, CLEAN, 0)
    assert "Status: clean" in text and "REGRESSION" not in text
    assert "New findings: none" in text
    assert "Edges added: 3 (imports 3); removed: 0" in text
    assert "Outside the expected scope: nothing reported" in text


def test_changed_rule_files_get_a_prominent_line(env):
    review = {**REVIEW, "rule_files_changed": ["enola/constraints/layers.yaml"]}
    lines = env.cr.report(review, CLEAN, 0).splitlines()
    assert lines[2].startswith(
        "!! RULE FILES CHANGED by this change: enola/constraints/layers.yaml"
    )


def test_a_stored_review_says_when_the_graph_was_reloaded_since(env):
    env.server.on(
        "POST",
        RECALL,
        (200, code_rows(stored(CLEAN, current_graph_snapshot_id="sha256:bbb"))),
    )
    answer = env.cr.stored_review(str(env.repo), env.rt)
    review = answer["review"]
    assert "The code graph was reloaded since this review" in env.cr.report(
        review, review["verdict"], review["exit_code"]
    )
    # The repository's name is the query and the filter: free text would filter it out.
    body = env.server.bodies(RECALL)[0]
    assert body["query"] == "proj" and body["code_query"] == {"operation": "review", "repo": "proj"}
    assert body["scope"] == ["code"] and body["datasets"] == [env.state["dataset"]]


def test_a_declined_base_is_reported_as_not_comparable(env):
    verdict = {"status": "declined", "diff": {"comparability": {"reason": "different config"}}}
    assert "Status: DECLINED, the base is not comparable: different config" in env.cr.report(
        REVIEW, verdict, 3
    )


def test_the_default_base_is_the_remote_default_branch(env):
    env.server.on("POST", ANALYZE, (200, check_reply(CLEAN)))
    missing = run(env, base="")
    assert not missing["ok"] and missing["code"] == "no_base"
    assert "name the base ref" in missing["message"]
    assert env.server.bodies(ANALYZE) == []

    import subprocess

    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk"],
        cwd=env.repo,
        check=True,
    )
    assert run(env, base="")["ok"]
    assert env.server.bodies(ANALYZE)[0]["params"] == {"base": "origin/trunk"}


# -- A server that cannot do it: one clear line ----------------------------------


def assert_one_clear_line(answer, *fragments):
    assert not answer["ok"]
    message = answer["message"]
    assert "\n" not in message and "Traceback" not in message
    assert "needs a local Cognee server 1.6.11+ with a path-indexed repository" in message
    for fragment in fragments:
        assert fragment in message, message


def test_a_server_without_the_route_is_named_and_not_posted_to(env):
    env.server.on("GET", OPENAPI, (200, SPEC_WITHOUT_ROUTE))
    assert_one_clear_line(run(env), "no /api/v1/code/analyze route", env.server.url)
    assert env.server.bodies(ANALYZE) == []
    # The verdict is cached per server: the next run asks nothing at all.
    before = len(env.server.calls)
    run(env)
    assert len(env.server.calls) == before


def test_a_cloud_server_with_a_url_indexed_repository(env, monkeypatch):
    for path in env.cg._STATE_DIR.glob("*.json"):
        path.unlink()
    env.cg.save_repo_state(
        {
            "spec": "https://github.com/acme/proj",
            "spec_kind": "url",
            "repo_root": "",
            "dataset": "d",
        }
    )
    cloud = FakeServer("https://tenant.cognee.example")
    monkeypatch.setattr(env.ca, "_HTTPS_OPENER", SimpleNamespace(open=cloud))
    answer = env.cr.run_review(str(env.repo), "main", {}, cloud.rt)
    assert_one_clear_line(
        answer, "indexed from a git URL", "shallow clone", "(a remote server)", cloud.url
    )
    assert cloud.calls == []


def test_a_checkout_that_is_not_indexed(env, tmp_path):
    other = make_repo(tmp_path / "other")
    answer = env.cr.run_review(str(other), "main", {}, env.rt)
    assert_one_clear_line(answer, "not indexed by path", "cognee-index-repo.sh")
    assert env.server.calls == []


def test_the_local_path_gate_refusing_the_repository(env):
    env.server.on(
        "POST",
        ANALYZE,
        (
            403,
            {"detail": "Server-side paths are not accepted for network callers [IngestionError]"},
        ),
    )
    assert_one_clear_line(
        run(env), "refused the repository path (HTTP 403", "ACCEPT_LOCAL_FILE_PATH=true"
    )


@pytest.mark.parametrize(
    "code,fragment",
    [
        ("shallow_clone", "shallow clone, which has no git history"),
        ("not_indexed", "code graph is not loaded on the server yet"),
        ("repository_unavailable", "cannot find the repository's directory"),
        ("no_code_repository", "holds no code repository"),
    ],
)
def test_a_repository_the_server_cannot_analyse(env, code, fragment):
    env.server.on("POST", ANALYZE, (422, {"detail": "server detail", "code": code}))
    answer = run(env)
    assert_one_clear_line(answer, fragment, "server detail")
    assert answer["code"] == code


def test_other_failures_keep_the_servers_reason(env):
    env.server.on(
        "POST", ANALYZE, (422, {"detail": "enola check: --base nope: no such commit or ref"})
    )
    answer = run(env, base="nope")
    assert not answer["ok"] and "no such commit or ref" in answer["message"]
    env.server.on("POST", ANALYZE, (404, {"detail": "Dataset 'x' not found or not accessible."}))
    assert "no write permission" in run(env)["message"]
    env.server.on("POST", ANALYZE, ConnectionRefusedError("refused"))
    assert "is unreachable" in run(env)["message"]


def test_a_spec_that_cannot_be_read_does_not_block_the_review(env):
    env.server.on("GET", OPENAPI, socket.timeout("slow"))
    env.server.on("POST", ANALYZE, (200, check_reply(CLEAN)))
    assert run(env)["ok"]
    # Unknown is not cached: a route-less server is still told apart later.
    env.server.on("GET", OPENAPI, (200, SPEC_WITHOUT_ROUTE))
    assert not run(env)["ok"]


# -- The script ------------------------------------------------------------------


@pytest.fixture
def script(env, suite, hook_module, monkeypatch):
    module = hook_module(suite, "cognee-review.py")
    monkeypatch.setattr(module._code_analysis, "_HTTPS_OPENER", SimpleNamespace(open=env.server))
    monkeypatch.setattr(module._code_analysis, "runtime", lambda: env.rt)
    monkeypatch.chdir(env.repo)
    return module


def test_the_script_exits_with_the_verdict(env, script, capsys):
    env.server.on("POST", ANALYZE, (200, check_reply(REGRESSION, 1)))
    assert script.main(["main", "--fail-on", "cycles,layers", "--max-spillover", "0"]) == 1
    assert "Status: REGRESSION" in capsys.readouterr().out
    assert env.server.bodies(ANALYZE)[0]["params"] == {
        "base": "main",
        "fail_on": ["cycles", "layers"],
        "max_spillover": 0,
    }
    env.server.on("POST", ANALYZE, (200, check_reply(CLEAN)))
    assert script.main(["main", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["output"]["status"] == "clean"


def test_the_script_prints_one_line_and_exits_2_when_it_cannot_run(env, script, capsys):
    env.server.on("GET", OPENAPI, (200, SPEC_WITHOUT_ROUTE))
    assert script.main(["main"]) == 2
    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0 and out.startswith("Cognee review needs a local Cognee server")
    assert script.main(["--bogus"]) == 2


def test_the_script_prints_the_stored_verdict(env, script, capsys):
    env.server.on("POST", RECALL, (200, code_rows(stored(REGRESSION, 1))))
    assert script.main(["--stored"]) == 0
    assert "Status: REGRESSION" in capsys.readouterr().out
    env.server.on("POST", RECALL, (200, code_rows({"operation": "review", "reviews": []})))
    assert script.main(["--stored"]) == 0
    assert "no stored verdict" in capsys.readouterr().out
    env.server.on("POST", RECALL, (422, {"detail": "Unsupported", "code": "unknown_operation"}))
    assert script.main(["--stored"]) == 2
    assert "no review CODE operation" in capsys.readouterr().out


# -- The next prompt ---------------------------------------------------------------


def reviewed(env, verdict, exit_code=0, **review):
    """A review the script ran: the marker is pending and the verdict is stored."""
    env.server.on("POST", ANALYZE, (200, check_reply(verdict, exit_code, **review)))
    assert run(env)["ok"]
    env.server.on("POST", RECALL, (200, code_rows(stored(verdict, exit_code, **review))))
    env.server.calls.clear()


def notice(env, **kwargs):
    return env.cr.prompt_notice(str(env.repo), lambda: env.rt, **kwargs)


def test_no_review_costs_no_request(env):
    assert notice(env) == ("", None)
    assert env.server.calls == []


def test_a_checkout_that_is_not_indexed_costs_nothing(env, tmp_path):
    other = make_repo(tmp_path / "other")
    assert env.cr.prompt_notice(str(other), lambda: env.rt) == ("", None)
    assert env.server.calls == []


def test_a_regression_is_shown_once_per_stored_verdict(env):
    reviewed(env, REGRESSION, 1)
    text, mark_shown = notice(env)
    assert text.splitlines()[0] == (
        "Cognee code review of proj against main (stored verdict, reviewed "
        "2026-11-26T17:17:25): REGRESSION"
    )
    assert "- [cycles] Cyclic dependency detected (2 modules)" in text
    assert "- 1 package(s) reached outside the declared scope (over the limit)" in text
    assert len(text) <= env.cr.NOTICE_MAX_CHARS
    # Not marked until it was printed: a block that did not fit is offered again.
    assert notice(env)[0] == text
    mark_shown()
    env.server.calls.clear()
    assert notice(env) == ("", None)
    assert env.server.calls == []

    # A new review of the same repository is a new verdict.
    reviewed(env, REGRESSION, 1, reviewed_at="2026-11-27T09:00:00+00:00")
    assert "reviewed 2026-11-27T09:00:00" in notice(env)[0]


def test_changed_rule_files_are_shown_without_a_regression(env):
    reviewed(env, CLEAN, rule_files_changed=["enola-intent.yaml"])
    text, mark_shown = notice(env)
    assert "REGRESSION" not in text
    assert "Rule files changed by the change itself: enola-intent.yaml" in text
    assert callable(mark_shown)


def test_a_clean_review_shows_nothing_and_is_settled(env):
    reviewed(env, CLEAN)
    assert notice(env) == ("", None)
    env.server.calls.clear()
    assert notice(env) == ("", None)
    assert env.server.calls == []


def test_a_server_without_the_operation_is_skipped_silently_and_remembered(env):
    reviewed(env, REGRESSION, 1)
    env.server.on(
        "POST", RECALL, (422, {"detail": "Unsupported CODE operation", "code": "unknown_operation"})
    )
    assert notice(env) == ("", None)
    assert len(env.server.calls) == 1
    # Even a later review does not ask this server for an hour.
    env.server.on("POST", ANALYZE, (200, check_reply(REGRESSION, 1)))
    assert run(env)["ok"]
    env.server.calls.clear()
    assert notice(env) == ("", None)
    assert env.server.calls == []


def test_the_read_is_bounded_and_given_up_after_three_prompts(env):
    reviewed(env, REGRESSION, 1)

    def slow(body):
        time.sleep(0.6)
        return 200, code_rows(stored(REGRESSION, 1))

    env.server.on("POST", RECALL, slow)
    for _ in range(env.cr.NOTICE_MAX_ATTEMPTS):
        started = time.monotonic()
        assert notice(env, timeout=0.1) == ("", None)
        assert time.monotonic() - started < 0.5
    env.server.calls.clear()
    assert notice(env, timeout=0.1) == ("", None)
    assert env.server.calls == []


# -- The prompt hook ---------------------------------------------------------------


@pytest.fixture
def hook(env, suite, hook_module, monkeypatch):
    module = hook_module(suite, "session-context-lookup.py")
    # hook_module reloads the plugin's modules: bind the fresh copies to the server.
    import _code_analysis
    import _code_graph

    monkeypatch.setattr(_code_analysis, "_HTTPS_OPENER", SimpleNamespace(open=env.server))
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    index_by_path(_code_graph, env.repo)
    env.ca, env.cr = _code_analysis, __import__("_code_review")
    monkeypatch.setenv("COGNEE_BASE_URL", env.server.url)
    return module


def test_the_hook_puts_the_block_in_front_of_the_recall_context(env, hook):
    reviewed(env, REGRESSION, 1)
    recall = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "Cognee memory: recall 1 memory",
        }
    }
    merged = hook._with_review_notice(recall, str(env.repo))
    context = merged["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Cognee code review of proj against main")
    assert context.endswith("\n\nCognee memory: recall 1 memory")
    assert merged["systemMessage"].startswith("Cognee code review of proj")
    # Shown: the next prompt gets the recall output untouched.
    assert hook._with_review_notice(recall, str(env.repo)) is recall


def test_the_hook_shows_the_block_when_recall_printed_nothing(env, hook):
    reviewed(env, REGRESSION, 1)
    merged = hook._with_review_notice(None, str(env.repo))
    assert merged["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "REGRESSION" in merged["hookSpecificOutput"]["additionalContext"]


def test_a_block_over_the_injection_budget_waits_for_the_next_prompt(env, hook, monkeypatch):
    reviewed(env, REGRESSION, 1)
    full = {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "x" * 990}
    }
    monkeypatch.setenv("COGNEE_INJECT_MAX_CHARS", "1024")
    assert hook._with_review_notice(full, str(env.repo)) is full
    monkeypatch.delenv("COGNEE_INJECT_MAX_CHARS")
    assert "REGRESSION" in json.dumps(hook._with_review_notice(full, str(env.repo)))


def test_the_hook_survives_a_broken_review_module(env, hook, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(env.cr, "prompt_notice", boom)
    output = {"hookSpecificOutput": {"additionalContext": "kept"}}
    assert hook._with_review_notice(output, str(env.repo)) is output
    assert os.path.isdir(env.repo)
