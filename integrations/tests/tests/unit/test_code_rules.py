"""Rule proposals: mined candidates, linted as drafts, accepted into the tree.

The server mines (``constraints_mine``) and lints (``constraints_lint``); the
candidates and lint reports below are what a real server returned (cognee
029d36a78, enola 0.5.4), cut down. The server is a fake opener.

Claude Code only.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from utils.code_analysis import (
    ANALYZE,
    OPENAPI,
    SPEC_WITHOUT_ROUTE,
    FakeServer,
    index_by_path,
    make_repo,
)


def candidate(rank: int, rule_id: str, statement: str, exceptions=(), witnesses=()) -> dict:
    return {
        "type": "candidate",
        "rank": rank,
        "family": "naming",
        "kind": "symbol",
        "identity": "naming||symbol|" + rule_id,
        "statement": statement,
        "numerator": 6,
        "denominator": 9,
        "confidence": 0.6666,
        "exceptions": list(exceptions),
        "witnesses": list(witnesses),
        "yaml": (
            'components:\n  - name: "mined"\n    kind: "symbol"\n'
            '    match:\n      - "inventory/**"\n'
            'rules:\n  - id: "%s"\n    require_name: "mined"\n    pattern: "Inventory*"\n'
            '    mode: "advisory"\n    because: "Mined regularity."\n' % rule_id
        ),
    }


PRICING = candidate(
    1,
    "mined-pricing",
    "6/9 symbol facts under inventory/ are named inventory/store.Inventory*",
    exceptions=[
        {
            "name": "inventory/pricing.compute_total",
            "file": "inventory/pricing.py",
            "detail": "name outside inventory/store.Inventory*",
        }
    ],
    witnesses=[{"name": "inventory/store.InventoryStore", "file": "inventory/store.py"}],
)
EMPTY = candidate(2, "mined-empty", "5/5 symbol facts have language containing go")
BILLING = candidate(
    3,
    "mined-billing",
    "4/5 symbol facts under billing/ are named Invoice*",
    exceptions=[
        {"name": "billing/invoice.make_invoice", "file": "billing/invoice.py", "detail": "x"}
    ],
)
REPORT = [
    {"type": "mining-report", "candidates": 3, "fact_count": 22},
    PRICING,
    {"type": "suppressed", "family": "allow-only", "below_support_floor": 2},
    EMPTY,
    BILLING,
]
LINT_OK = (
    "\nproj: no declaration yet; linting the draft alone.\n"
    "  /tmp/x/declaration.yaml: 1 component(s), 1 rule(s), 0 exemption(s)\n"
)
LINT_EMPTY = (
    "\nproj: no declaration yet; linting the draft alone.\n"
    "  /var/folders/T/cognee-enola-analysis-wtwdwcji/declaration.yaml: 1 component(s), "
    "1 rule(s), 0 exemption(s)\n"
    "    draft adds components: mined\n\n"
    "  mined   0 member(s)  <- matches nothing; every rule naming it holds vacuously\n\n"
    "--fail-empty: 1 component or layer selects nothing; each counts as a problem.\n\n"
    "FAIL — 1 validation problem.\n"
)


def reply(analysis: str, output, exit_code: int = 0) -> dict:
    return {
        "analysis": analysis,
        "dataset_id": "10d70858-43cf-5f39-919b-5aa182eda938",
        "data_ids": [],
        "enola_version": "0.5.4",
        "exit_code": exit_code,
        "output": output,
    }


def analyses(body):
    """mine answers the report; lint fails the candidate whose draft is EMPTY's."""
    if body["analysis"] == "constraints_mine":
        return 200, reply("constraints_mine", REPORT)
    assert body["params"]["fail_empty"] is True
    if "mined-empty" in body["params"]["declaration"]:
        return 200, reply("constraints_lint", LINT_EMPTY, 1)
    return 200, reply("constraints_lint", LINT_OK)


@pytest.fixture
def env(suite, hook_module, monkeypatch, tmp_path):
    if suite.name != "claude-code":
        pytest.skip("the rules script ships with the Claude Code plugin")
    rules = hook_module(suite, "cognee-rules.py")
    ca = rules._code_analysis
    server = FakeServer().on("POST", ANALYZE, analyses)
    monkeypatch.setattr(ca, "_HTTPS_OPENER", SimpleNamespace(open=server))
    monkeypatch.setattr(ca, "runtime", lambda: server.rt)
    monkeypatch.setattr(rules, "stored_lessons", lambda cwd: ([], "coding memory is off"))
    repo = make_repo(tmp_path / "proj")
    state = index_by_path(ca._code_graph, repo)
    monkeypatch.chdir(repo)
    return SimpleNamespace(rules=rules, ca=ca, server=server, repo=repo, state=state, rt=server.rt)


def propose(env, lessons=(), limit=20, **params):
    return env.rules.propose(str(env.repo), env.rt, params, limit, list(lessons))


def ids(result) -> list:
    return [entry["candidate"]["statement"].split(" ")[0] for entry in result["shown"]]


def test_every_candidate_is_linted_as_a_draft_and_none_is_dropped(env):
    result = propose(env, min_support=5)
    assert [e["candidate"] for e in result["shown"]] == [PRICING, EMPTY, BILLING]
    assert [e["lint"]["ok"] for e in result["shown"]] == [True, False, True]
    mine, *lints = env.server.bodies(ANALYZE)
    assert mine == {
        "analysis": "constraints_mine",
        "params": {"min_support": 5},
        "dataset": env.state["dataset"],
    }
    assert [b["params"]["declaration"] for b in lints] == [
        c["yaml"] for c in (PRICING, EMPTY, BILLING)
    ]
    assert result["suppressed"] == [REPORT[2]]


def test_a_failing_candidate_is_shown_with_its_reason(env, capsys):
    assert env.rules.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "Cognee rules: 3 candidate rule(s) for proj" in out
    assert "  lint: FAILS" in out
    assert (
        "    - --fail-empty: 1 component or layer selects nothing; each counts as a problem." in out
    )
    assert "matches nothing" in out
    # The server's scratch path is not part of the reason.
    assert "/var/folders" not in out
    assert out.count("  lint: passes") == 2
    assert "  support: 6/9 (confidence 0.67), family naming" in out
    assert "    - inventory/pricing.compute_total (inventory/pricing.py): name outside" in out
    assert "  rule file: enola/constraints/mined-pricing.yaml" in out


def test_lint_problem_lines(env):
    text = (
        "\nproj: linting.\n  /tmp/a/declaration.yaml: 0 component(s), 1 rule(s), 0 exemption(s)\n"
        '  /tmp/a/declaration.yaml: rules[0] (bad): require "nope" names no declared component\n'
        "  /tmp/a/declaration.yaml: rules[0] (bad): needs a because\n\n"
        "FAIL — 2 validation problems.\n"
    )
    assert env.rules.lint_problems(text) == [
        'rules[0] (bad): require "nope" names no declared component',
        "rules[0] (bad): needs a because",
    ]
    assert env.rules.lint_problems("FAIL — 1 validation problem.") == [
        "FAIL — 1 validation problem."
    ]
    assert env.rules.lint_problems("") == ["lint reported problems (no detail)"]


def test_a_lint_call_that_fails_marks_the_candidate_failing(env):
    def flaky(body):
        if body["analysis"] == "constraints_lint":
            return 500, {"detail": "boom"}
        return 200, reply("constraints_mine", REPORT)

    env.server.on("POST", ANALYZE, flaky)
    result = propose(env)
    assert len(result["shown"]) == 3
    assert all(not e["lint"]["ok"] for e in result["shown"])
    assert "lint could not run" in result["shown"][0]["lint"]["problems"][0]


def lesson(body, kind="convention", files=(), symbols=()) -> dict:
    return {"lesson_type": kind, "body": body, "files": list(files), "symbols": list(symbols)}


def test_candidates_with_related_lessons_come_first(env):
    lessons = [
        lesson("Invoices are built in billing only.", files=["invoice.py"]),
        lesson("Unrelated: run the tests with uv.", files=["README.md"]),
        lesson("compute_total must stay pure.", kind="constraint"),
    ]
    result = propose(env, lessons)
    assert [e["candidate"] for e in result["shown"]] == [PRICING, BILLING, EMPTY]
    assert [len(e["lessons"]) for e in result["shown"]] == [1, 1, 0]
    assert result["shown"][0]["lessons"][0]["body"] == "compute_total must stay pure."
    # A stamped symbol on the lesson counts like a name in its text.
    stamped = lesson(
        "Keep it small.",
        symbols=[{"name": "billing/invoice.make_invoice", "file": "billing/invoice.py"}],
    )
    assert [e["candidate"] for e in propose(env, [stamped])["shown"]][0] == BILLING


def test_the_limit_bounds_what_is_linted_and_the_rest_is_counted(env, capsys):
    result = propose(env, limit=1)
    assert len(result["shown"]) == 1 and result["not_shown"] == 2
    assert len(env.server.bodies(ANALYZE)) == 2  # mine + one lint
    assert env.rules.main(["list", "--limit", "1"]) == 0
    assert "2 more not shown (raise --limit)" in capsys.readouterr().out


def test_accept_writes_the_rule_file_and_nothing_else(env, capsys):
    shown = propose(env)["shown"]
    env.server.calls.clear()
    wanted = shown[0]["id"]
    assert env.rules.main(["accept", wanted]) == 0
    path = env.repo / "enola" / "constraints" / "mined-pricing.yaml"
    assert path.read_text(encoding="utf-8") == PRICING["yaml"]
    assert "not committed" in capsys.readouterr().out
    # No request, no commit, nothing staged.
    assert env.server.calls == []
    import subprocess

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=env.repo, capture_output=True, text=True
    ).stdout
    assert status.strip() == "?? enola/"
    # A second accept never overwrites.
    assert env.rules.main(["accept", wanted]) == 1
    assert "already exists" in capsys.readouterr().out
    assert env.rules.main(["accept", wanted, "--name", "pricing-names"]) == 0
    assert (path.parent / "pricing-names.yaml").exists()


def test_a_candidate_that_fails_lint_is_not_written(env, capsys):
    failing = [e for e in propose(env)["shown"] if not e["lint"]["ok"]][0]
    assert env.rules.main(["accept", failing["id"]]) == 1
    assert "fails lint and was not written" in capsys.readouterr().out
    assert not (env.repo / "enola").exists()
    assert env.rules.main(["accept", "0000000000"]) == 1
    assert "no candidate 0000000000 in the last list" in capsys.readouterr().out


def test_a_rejected_candidate_is_not_proposed_again(env, capsys):
    shown = propose(env)["shown"]
    assert env.rules.main(["reject", shown[2]["id"]]) == 0
    assert "will not be proposed again" in capsys.readouterr().out
    again = propose(env)
    assert [e["candidate"] for e in again["shown"]] == [PRICING, EMPTY]
    assert again["rejected"] == 1
    assert env.rules.main(["list"]) == 0
    assert "1 rejected earlier" in capsys.readouterr().out
    saved = json.loads(env.ca.repo_state_path(env.state["repo_root"], "rules-").read_text())
    assert saved["rejected"] == [shown[2]["id"]]
    assert env.rules.main(["reject", "ffffffffff"]) == 1


def test_a_server_that_cannot_mine_gets_one_line(env, capsys, tmp_path, monkeypatch):
    env.server.on("GET", OPENAPI, (200, SPEC_WITHOUT_ROUTE))
    assert env.rules.main(["list"]) == 2
    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0
    assert out.startswith("Cognee rules needs a local Cognee server 1.6.11+ with a path-indexed")
    assert "no /api/v1/code/analyze route" in out
    monkeypatch.chdir(make_repo(tmp_path / "other"))
    assert env.rules.main(["list"]) == 2
    assert "not indexed by path" in capsys.readouterr().out
    assert env.rules.main(["accept", "x"]) == 2
    assert os.path.isdir(tmp_path / "other") and not (tmp_path / "other" / "enola").exists()


def test_lessons_are_skipped_with_a_note_when_coding_memory_is_off(suite, hook_module):
    if suite.name != "claude-code":
        pytest.skip("the rules script ships with the Claude Code plugin")
    rules = hook_module(suite, "cognee-rules.py")
    lessons, note = rules.stored_lessons(os.getcwd())
    assert lessons == [] and "coding memory is off" in note
