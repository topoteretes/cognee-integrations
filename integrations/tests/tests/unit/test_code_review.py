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


def test_findings_below_the_confidence_floor_are_explained(env):
    """enola counts a --fail-on finding only at its confidence floor (default 1): a
    heuristic finding is reported, the verdict stays clean, and the report says why."""
    heuristic = dict(DEAD, confidence=0.8)
    verdict = dict(
        CLEAN,
        policy={"fail_explainers": ["dead-code"], "min_confidence": 1, "warn_only": False},
        diff={"findings_new": [heuristic, dict(CYCLE, confidence=1)]},
    )
    text = env.cr.report(REVIEW, verdict, 0)
    assert "Status: clean" in text
    assert "(billing/invoice.py:6), confidence 0.8" in text
    assert (
        "1 new finding(s) of the --fail-on explainers did not count: enola's confidence in "
        "them (highest 0.8) is below the floor 1. Pass --min-confidence 0.8 to count them."
    ) in text
    # Without a --fail-on policy, or once the floor admits the finding, nothing to explain.
    assert "did not count" not in env.cr.report(REVIEW, dict(verdict, policy={}), 0)
    counted = dict(verdict, policy={"fail_explainers": ["dead-code"], "min_confidence": 0.8})
    assert "did not count" not in env.cr.report(REVIEW, counted, 0)


def test_the_script_passes_the_confidence_floor(env, script):
    env.server.on("POST", ANALYZE, (200, check_reply(CLEAN)))
    script.main(["main", "--fail-on", "dead-code", "--min-confidence", "0.8"])
    assert env.server.bodies(ANALYZE)[0]["params"] == {
        "base": "main",
        "fail_on": ["dead-code"],
        "min_confidence": 0.8,
    }


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


# -- Where to look -----------------------------------------------------------------


def symbol(name: str, line: int, lines: int = 3) -> dict:
    return {
        "id": (name.encode().hex() + "0" * 32)[:32],
        "name": "pricing." + name,
        "symbol_kind": "function",
        "file": "pricing.py",
        "line": line,
        "end_line": line + lines - 1,
        "lines": lines,
    }


#: Seven changed symbols; ``impact`` is asked for the five largest.
SYMBOLS = [
    symbol(name, 10 * n, lines)
    for n, (name, lines) in enumerate(
        [
            ("tiny", 1),
            ("line_total", 9),
            ("total", 8),
            ("discount", 7),
            ("tax", 6),
            ("fmt", 5),
            ("nit", 2),
        ],
        start=1,
    )
]
DEPENDENTS = {
    "pricing.line_total": (2, 1),
    "pricing.total": (4, 3),
    "pricing.discount": (0, 0),
    "pricing.tax": (4, 1),
    "pricing.fmt": (1, 1),
}


def analysis_reply(output, exit_code: int = 0) -> dict:
    return {"analysis": "x", "exit_code": exit_code, "output": output, "review": None}


def impact_output(target: str) -> dict:
    total, direct = DEPENDENTS[target]
    by_depth = {}
    if direct:
        by_depth["1"] = [{"name": f"d{n}"} for n in range(direct)]
    if total - direct:
        by_depth["2"] = [{"name": f"t{n}"} for n in range(total - direct)]
    return {"target": target, "by_depth": by_depth}


@pytest.fixture
def looking(env, monkeypatch):
    """A change in the checkout's working tree and a server that answers the
    extra analyses. ``env.answers[analysis]`` is a reply, a callable or a failure."""
    (env.repo / "pricing.py").write_text("def line_total(price, qty):\n    return 0\n")
    env.review = dict(
        REVIEW, base_commit=env.cg._run_git(["rev-parse", "HEAD"], str(env.repo)).strip()
    )
    env.answers = {
        "locate": lambda p: (200, analysis_reply({"diff": {"files": [{"symbols": SYMBOLS}]}})),
        "impact": lambda p: (200, analysis_reply(impact_output(p["target"]))),
        "touched": lambda p: (
            200,
            analysis_reply(
                {
                    "ranking": [
                        {"id": SYMBOLS[4]["id"], "name": "pricing.tax", "count": 2},
                        {"id": SYMBOLS[0]["id"], "name": "pricing.tiny", "count": 1},
                    ]
                }
            ),
        ),
    }

    def answer(body):
        reply = env.answers[body["analysis"]]
        return reply(body["params"]) if callable(reply) else reply

    env.server.on("POST", ANALYZE, answer)
    env.refreshed = []
    monkeypatch.setattr(env.cr, "_refresh_graph", lambda *a, **k: env.refreshed.append(1) or "")
    return env


def look(env, verdict=CLEAN, **kwargs) -> dict:
    return env.cr.where_to_look(env.state, env.rt, env.review, verdict, **kwargs)


def asked(env) -> list:
    return [body["analysis"] for body in env.server.bodies(ANALYZE)]


def test_where_to_look_ranks_the_changed_symbols_by_what_depends_on_them(looking):
    env = looking
    where = look(env)
    # One call after another: the diff's symbols, impact of the five largest, the fix history.
    assert asked(env) == ["locate", *["impact"] * 5, "touched"]
    bodies = env.server.bodies(ANALYZE)
    assert "+    return 0" in bodies[0]["params"]["diff"]
    assert [b["params"]["target"] for b in bodies[1:6]] == [
        "pricing.line_total",
        "pricing.total",
        "pricing.discount",
        "pricing.tax",
        "pricing.fmt",
    ]
    assert bodies[6]["params"] == {
        "rev": env.review["base_commit"],
        "subject": env.cr.FIX_SUBJECT,
        "top": 0,
    }
    assert (where["impact_asked"], where["impact_left_out"]) == (5, 2)
    assert len(where["changed_symbols"]) == 7
    assert env.cr.where_lines(where) == [
        "Where to look:",
        "  1. pricing.total (pricing.py:30): 4 symbol(s) depend on it (3 directly)",
        "  2. pricing.tax (pricing.py:50): 4 symbol(s) depend on it (1 directly); "
        "changed by 2 fix commit(s) before",
        "  3. pricing.line_total (pricing.py:20): 2 symbol(s) depend on it (1 directly)",
        "  4. pricing.fmt (pricing.py:60): 1 symbol(s) depend on it (1 directly)",
        # Impact was not asked for it, but a fix changed it before.
        "  5. pricing.tiny (pricing.py:10): changed by 1 fix commit(s) before",
        "  (impact was asked for the 5 largest of 7 changed symbols)",
    ]
    # pricing.discount and pricing.nit: nothing depends on them, no fix: not places.
    assert where["more_places"] == 0 and where["unavailable"] == []


def test_findings_removed_symbols_and_the_scope_come_from_the_verdict(looking):
    env = looking
    env.answers["locate"] = (200, analysis_reply({"diff": {"files": [{"symbols": SYMBOLS[1:3]}]}}))
    finding = {
        "title": "Dead code candidate: function pricing.unused",
        "source": "dead-code",
        "evidence": [{"file": "pricing.py", "symbol": "pricing.unused", "line": 70}],
    }
    on_changed = dict(
        CYCLE, evidence=[{"file": "pricing.py", "symbol": "pricing.total", "line": 30}]
    )
    verdict = dict(
        REGRESSION,
        failures=[on_changed],
        diff={
            "findings_new": [on_changed, finding],
            "findings_resolved": [DEAD],
            "facts_removed": [
                {"kind": "symbol", "name": "pricing.legacy", "file": "pricing.py", "line": 80},
                {"kind": "dependency", "name": "pricing -> os"},
            ],
        },
    )
    where = look(env, verdict, target="pricing")
    assert env.cr.where_lines(where) == [
        "Where to look:",
        "  1. pricing.total (pricing.py:30): 4 symbol(s) depend on it (3 directly); "
        "new finding [cycles]",
        "  2. pricing.line_total (pricing.py:20): 2 symbol(s) depend on it (1 directly)",
        "  3. pricing.unused (pricing.py:70): new finding [dead-code]",
        "  4. pricing.legacy (pricing.py:80): removed by this change; "
        "check what still refers to it",
        "Resolved findings: 1",
        "  - [dead-code] Dead code candidate: function billing/invoice.unused_helper "
        "(billing/invoice.py:6)",
        "Removed symbols: pricing.legacy",
        "Reached outside pricing: 1 package(s) reached outside the declared scope (over the limit)",
    ]
    # Without --target the verdict's scope line is the report's own, not repeated here.
    assert not any(
        line.startswith("Reached outside") for line in env.cr.where_lines(look(env, verdict))
    )


def test_at_most_five_places_and_the_rest_is_counted(looking):
    env = looking
    many = [symbol(f"f{n}", n + 1, 9 - n) for n in range(5)]
    env.answers["locate"] = (200, analysis_reply({"diff": {"files": [{"symbols": many}]}}))
    env.answers["impact"] = (200, analysis_reply({"by_depth": {"1": [{"name": "x"}]}}))
    env.answers["touched"] = (200, analysis_reply({"ranking": []}))
    removed = [{"kind": "symbol", "name": f"pricing.gone{n}", "file": "pricing.py"} for n in (1, 2)]
    where = look(env, dict(CLEAN, diff={"facts_removed": removed}))
    lines = env.cr.where_lines(where)
    assert len(where["places"]) == env.cr.MAX_PLACES == 5 and where["more_places"] == 2
    assert "  (2 more place(s) had a reason; --json lists all the pieces)" in lines


def test_nothing_to_look_at_says_so(looking):
    env = looking
    env.answers["impact"] = (200, analysis_reply({"by_depth": {}}))
    env.answers["touched"] = (200, analysis_reply({"ranking": []}))
    where = look(env)
    assert where["places"] == []
    assert env.cr.where_lines(where)[1].startswith("  nothing stands out: 7 changed symbol(s)")


@pytest.mark.parametrize(
    ("failing", "calls", "line"),
    [
        (
            "locate",
            ["locate"],
            "Not available: changed symbols: Cognee review: the server could not run locate "
            "(HTTP 500): enola crashed",
        ),
        (
            "touched",
            ["locate", *["impact"] * 5, "touched"],
            "Not available: fix history: Cognee review: the server could not run touched "
            "(HTTP 500): enola crashed",
        ),
    ],
)
def test_a_part_that_fails_costs_one_line_and_nothing_else(looking, failing, calls, line):
    env = looking
    env.answers[failing] = (500, {"detail": "enola crashed"})
    where = look(env)
    assert asked(env) == calls
    lines = env.cr.where_lines(where)
    assert [text for text in lines if text.startswith("Not available")] == [line]
    if failing == "touched":  # the ranking by dependents is still there
        assert lines[1].startswith("  1. pricing.total (pricing.py:30): 4 symbol(s)")
        assert "fix commit" not in "\n".join(lines)


def test_impact_failing_for_some_symbols_is_one_line(looking):
    env = looking

    def impact(params):
        if params["target"] in ("pricing.total", "pricing.fmt"):
            return 500, {"detail": "boom"}
        return 200, analysis_reply(impact_output(params["target"]))

    env.answers["impact"] = impact
    lines = env.cr.where_lines(look(env))
    assert lines[1].startswith("  1. pricing.tax (pricing.py:50): 4 symbol(s) depend on it")
    unavailable = [text for text in lines if text.startswith("Not available")]
    assert len(unavailable) == 1 and unavailable[0].startswith(
        "Not available: impact of pricing.total, pricing.fmt: "
    )


def test_the_extra_calls_share_one_time_budget(looking):
    env = looking
    where = look(env, budget=0.0)
    assert asked(env) == []  # no time left: nothing is asked, the verdict stands
    assert where["places"] == []
    assert where["unavailable"] == ["changed symbols: no time left in the review's budget"]

    def slow(params):
        time.sleep(0.3)
        return 200, analysis_reply({"diff": {"files": [{"symbols": SYMBOLS[:2]}]}})

    env.answers["locate"] = slow
    where = look(env, budget=0.2)
    assert asked(env) == ["locate"]  # the budget ran out during it: impact is not started
    assert any(line.startswith("impact of ") for line in where["unavailable"])


def test_the_graph_is_refreshed_before_the_review_when_asked(looking):
    """Before, so the stored verdict and the analyses after it describe one graph."""
    env = looking
    order: list = []
    env.answers["check"] = lambda p: order.append("check") or (200, check_reply(CLEAN))
    env.cr._refresh_graph = lambda *a, **k: order.append("refresh") or ""
    assert env.cr.run_review(str(env.repo), "main", {}, env.rt)["graph_lag"] == ""
    assert order == ["check"]
    order.clear()
    env.cr._refresh_graph = lambda *a, **k: order.append("refresh") or "unreachable"
    answer = env.cr.run_review(str(env.repo), "main", {}, env.rt, refresh=True)
    assert order == ["refresh", "check"]
    assert answer["ok"] and answer["graph_lag"] == "unreachable"


def test_a_graph_that_could_not_be_refreshed_is_said(looking):
    env = looking
    where = look(env, graph_lag="unreachable")
    assert where["unavailable"] == [
        "code graph refresh: unreachable; symbols are read from the graph as last indexed"
    ]
    assert len(where["places"]) == 5


def test_no_diff_against_the_merge_base_asks_nothing(looking):
    env = looking
    env.cg._run_git(["checkout", "--", "pricing.py"], str(env.repo))
    where = look(env)
    assert asked(env) == []
    assert where["unavailable"][0].startswith("changed symbols: git shows no diff")


def test_the_script_prints_where_to_look_after_the_verdict(looking, script, capsys, monkeypatch):
    env = looking
    # ``script`` loaded the modules again: the refresh of its own copy is the one to fake.
    monkeypatch.setattr(script._code_review, "_refresh_graph", lambda *a, **k: "")
    env.answers["check"] = (200, check_reply(CLEAN, base_commit=env.review["base_commit"]))
    assert script.main(["main"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("Cognee review: proj against main")
    report_end = out.index("Outside the expected scope: nothing reported")
    assert out[report_end + 1] == "Where to look:"
    assert out[report_end + 2].startswith("  1. pricing.total (pricing.py:30)")

    assert script.main(["main", "--json"]) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["output"] == CLEAN
    where = reply["where_to_look"]
    assert [place["symbol"] for place in where["places"]][:2] == ["pricing.total", "pricing.tax"]
    assert where["places"][0] == {
        "symbol": "pricing.total",
        "file": "pricing.py",
        "line": 30,
        "reasons": ["4 symbol(s) depend on it (3 directly)"],
    }
    assert {
        "changed_symbols",
        "resolved_findings",
        "removed_symbols",
        "outside_scope",
        "unavailable",
    } <= set(where)

    env.server.calls.clear()
    assert script.main(["main", "--no-where"]) == 0
    assert "Where to look" not in capsys.readouterr().out
    assert asked(env) == ["check"]


def test_a_declined_review_asks_for_nothing_more(looking, script, capsys):
    env = looking
    declined = dict(CLEAN, diff={"comparability": {"comparable": False, "reason": "no history"}})
    env.answers["check"] = (200, check_reply(declined, 3))
    assert script.main(["main"]) == 3
    assert "Where to look" not in capsys.readouterr().out
    assert asked(env) == ["check"]


def test_a_manual_review_leaves_no_places_for_the_notice(looking):
    """Only the automatic review's places go to the next prompt; a later verdict drops them."""
    env = looking
    env.answers["check"] = (200, check_reply(CLEAN))
    assert run(env)["ok"]
    env.cr.note_where(
        str(env.repo),
        REVIEW,
        {
            "places": [
                {
                    "symbol": "a.b",
                    "file": "a.py",
                    "line": 3,
                    "reasons": ["2 symbol(s) depend on it (1 directly)"],
                }
            ]
        },
    )
    env.server.on("POST", RECALL, (200, code_rows(stored(CLEAN))))
    text, _ = notice(env)
    assert text.endswith("Where to look:\n- a.b (a.py:3): 2 symbol(s) depend on it (1 directly)")
    # A new review of the repository: the places were another verdict's.
    env.answers["check"] = (200, check_reply(CLEAN, reviewed_at="2026-11-28T09:00:00+00:00"))
    assert run(env)["ok"]
    env.server.on(
        "POST", RECALL, (200, code_rows(stored(CLEAN, reviewed_at="2026-11-28T09:00:00+00:00")))
    )
    assert notice(env) == ("", None)


def test_a_regression_notice_carries_the_places_too(looking):
    env = looking
    env.answers["check"] = (200, check_reply(REGRESSION, 1))
    assert run(env)["ok"]
    env.cr.note_where(
        str(env.repo),
        REVIEW,
        {"places": [{"symbol": "a.b", "file": "", "reasons": ["new finding [cycles]"]}]},
    )
    env.server.on("POST", RECALL, (200, code_rows(stored(REGRESSION, 1))))
    text, _ = notice(env)
    assert ": REGRESSION" in text.splitlines()[0]
    assert text.endswith("Where to look:\n- a.b: new finding [cycles]")
