"""Matched outcomes, held-out citations, bad memories and honest usage accounting."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "claude-code/skills/cognee-report/scripts"
spec = importlib.util.spec_from_file_location("outcome_eval", SCRIPTS / "outcome_eval.py")
oe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oe)


def suite():
    return {
        "model": "fixture-only",
        "max_tokens": 100,
        "variants": ["no_memory", "cognee"],
        "cases": [
            {
                "id": "decision",
                "prompt": "Which store?",
                "memory": [{"id": "current", "text": "store=postgres"}],
                "expected": {
                    "store": {
                        "value": "postgres",
                        "sources": ["current"],
                        "false_values": ["redis"],
                    }
                },
            }
        ],
    }


def test_citations_must_support_actual_claim_and_be_retrieved():
    case = suite()["cases"][0]
    wrong = oe.score(
        case,
        {
            "claims": [{"id": "store", "value": "redis", "citations": ["current"]}],
            "retrieved_ids": ["current"],
        },
    )
    assert not wrong["task_success"] and wrong["false_memory_adoptions"] == 1
    assert wrong["supported_citations"] == 0
    fabricated = oe.score(
        case, {"claims": [{"id": "store", "value": "postgres", "citations": ["current"]}]}
    )
    assert fabricated["task_success"] and fabricated["supported_citations"] == 0
    assert oe.score(case, {})["citations"] == 0


def test_usage_dedupes_inclusive_parents_but_keeps_orphan_children():
    rows = [
        {
            "source": "server",
            "id": "op",
            "tokens_in": 10,
            "tokens_out": 2,
            "cost_usd": 0.2,
            "includes_children": True,
        },
        {
            "source": "server",
            "id": "child",
            "parent_id": "op",
            "tokens_in": 10,
            "tokens_out": 2,
            "cost_usd": 0.2,
        },
        {"source": "client", "id": "op", "tokens_in": 5, "tokens_out": 3, "cost_usd": 0.1},
    ]
    totals = oe.usage_totals(rows + [rows[0]])
    assert totals["total_tokens"] == 20 and totals["cost_usd"] == pytest.approx(0.3)
    assert oe.usage_totals(rows[1:])["total_tokens"] == 20
    assert oe.usage_totals([{"source": "server", "id": "missing"}])["cost_usd"] is None
    with pytest.raises(ValueError, match="conflicting"):
        oe.usage_totals(rows + [{**rows[0], "tokens_in": 99}])


def test_usage_rejects_cycles_and_nonfinite_values():
    with pytest.raises(ValueError, match="cycle"):
        oe.usage_totals(
            [
                {"source": "server", "id": "a", "parent_id": "b"},
                {"source": "server", "id": "b", "parent_id": "a"},
            ]
        )
    assert (
        oe.usage_totals([{"source": "client", "id": "a", "cost_usd": float("nan")}])["cost_usd"]
        is None
    )


def test_repeated_smoke_trials_are_isolated_and_matched(tmp_path):
    result = oe.run_trials(
        suite(),
        [sys.executable, str(SCRIPTS / "outcome_smoke_runner.py")],
        tmp_path / "run",
        repetitions=3,
    )
    assert result["matched_blocks"] == 3 and result["repeated"]
    assert result["variants"]["no_memory"]["task_success_rate"] == 0
    assert result["variants"]["cognee"]["task_success_rate"] == 1
    assert result["variants"]["cognee"]["citation_correctness"] == 1
    assert (
        result["variants"]["cognee"]["latency_p95_ms"]
        >= result["variants"]["cognee"]["latency_p50_ms"]
    )
    records = [
        json.loads(line) for line in (tmp_path / "run/trials.jsonl").read_text().splitlines()
    ]
    assert len({r["session_id"] for r in records}) == 6
    assert all(r["recall_budget_seconds"] == 45 for r in records)
    assert "postgres" not in (tmp_path / "run/trials.jsonl").read_text()
    assert len(list((tmp_path / "run").iterdir())) == 2
    with pytest.raises(ValueError, match="duplicate"):
        oe.summarize(records + [records[0]])
    for identity in ("session_id", "dataset_name"):
        reused = [dict(row) for row in records]
        reused[1][identity] = reused[0][identity]
        with pytest.raises(ValueError, match="isolation"):
            oe.summarize(reused)
    records[0]["model"] = "another-model"
    with pytest.raises(ValueError, match="matched"):
        oe.summarize(records)


def test_timeouts_stay_in_denominator_and_usage_is_unknown(tmp_path):
    result = oe.run_trials(
        suite(),
        [sys.executable, "-c", "import time;time.sleep(1)"],
        tmp_path / "run",
        repetitions=2,
        timeout=0.05,
    )
    for row in result["variants"].values():
        assert row["trials"] == 2 and row["failures"] == 2 and row["task_success_rate"] == 0
        assert row["total_cost_usd"] is None and row["total_tokens"] is None
        assert row["citation_correctness"] is None


def test_recall_over_budget_fails_even_when_answer_is_correct(tmp_path):
    program = (
        "import json,sys;r=json.load(sys.stdin);print(json.dumps(dict(r,recall_ms=45001,"
        "claims=[dict(id='store',value='postgres')],retrieved_ids=[])))"
    )
    result = oe.run_trials(
        suite(), [sys.executable, "-c", program], tmp_path / "run", repetitions=2
    )
    assert result["variants"]["cognee"]["task_success_rate"] == 0
    assert result["variants"]["cognee"]["failures"] == 2


def test_runner_never_receives_oracle_and_no_memory_has_empty_snapshot(tmp_path):
    program = (
        "import json,sys;r=json.load(sys.stdin);assert 'expected' not in str(r);"
        "assert r['variant']!='no_memory' or not r['memory_seed'];"
        "print(json.dumps(dict(r,recall_ms=0,claims=[],retrieved_ids=[])))"
    )
    result = oe.run_trials(
        suite(), [sys.executable, "-c", program], tmp_path / "run", repetitions=2
    )
    assert all(row["failures"] == 0 for row in result["variants"].values())


def test_functional_verifier_checks_workspace_independently(tmp_path):
    data = suite()
    data["cases"][0]["files"] = {"answer.py": "value = 'wrong'\n"}
    data["cases"][0]["verify"] = [
        sys.executable,
        "-c",
        "from answer import value; assert value == 'right'",
    ]
    result = oe.run_trials(
        data,
        [sys.executable, str(SCRIPTS / "outcome_smoke_runner.py")],
        tmp_path / "run",
        repetitions=2,
    )
    assert result["variants"]["cognee"]["task_success_rate"] == 0


def test_invalid_runner_json_shape_is_recorded_as_failure(tmp_path):
    report = oe.run_trials(
        suite(), [sys.executable, "-c", "print('[]')"], tmp_path / "run", repetitions=2
    )
    assert report["variants"]["cognee"]["failures"] == 2


def test_repository_trials_start_from_the_same_clean_commit(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "answer.txt").write_text("pinned fixture")
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "answer.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        check=True,
    )
    data = suite()
    data["repository"] = {"path": str(source), "revision": "HEAD"}
    program = (
        "import json,sys;from pathlib import Path;r=json.load(sys.stdin);"
        "assert Path('answer.txt').read_text()=='pinned fixture';"
        "assert not Path('marker').exists();Path('marker').touch();"
        "print(json.dumps(dict(r,recall_ms=0,claims=[],retrieved_ids=[])))"
    )
    result = oe.run_trials(data, [sys.executable, "-c", program], tmp_path / "run", repetitions=2)
    assert all(row["failures"] == 0 for row in result["variants"].values())
    assert not (source / "marker").exists()


@pytest.fixture
def live_runner(monkeypatch):
    monkeypatch.setitem(sys.modules, "outcome_eval", oe)
    spec = importlib.util.spec_from_file_location(
        "outcome_cognee_runner", SCRIPTS / "outcome_cognee_runner.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("EVAL_MODEL_URL", "https://model.example/chat/completions")
    monkeypatch.setenv("EVAL_MODEL_API_KEY", "test-model-key")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://memory.example")
    monkeypatch.setenv("COGNEE_API_KEY", "test-memory-key")
    return module


def live_request(variant):
    return {
        "trial_id": "trial",
        "variant": variant,
        "model": "pinned-model",
        "max_tokens": 100,
        "seed": 42,
        "session_id": "eval_trial",
        "dataset_name": "eval_trial",
        "task": {"prompt": "Which store? Use claim id store."},
        "memory_seed": [{"id": "current", "text": "store=postgres"}],
    }


def completion():
    return {
        "id": "completion",
        "model": "pinned-model",
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"claims": [{"id": "store", "value": "postgres", "citations": ["current"]}]}
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.01},
    }


def test_live_baseline_never_contacts_cognee(live_runner, monkeypatch):
    calls = []

    def request(url, key, payload=None, **kwargs):
        calls.append((url, payload))
        return completion()

    monkeypatch.setattr(live_runner, "request_json", request)
    result = live_runner.run(live_request("no_memory"))
    assert len(calls) == 1 and calls[0][0].startswith("https://model.example")
    assert calls[0][1]["model"] == "pinned-model" and calls[0][1]["max_tokens"] == 100
    assert result["retrieved_ids"] == [] and result["usage"][0]["cost_usd"] == 0.01


def test_live_cognee_prepares_only_isolated_memory_and_enforces_deadline(live_runner, monkeypatch):
    from types import SimpleNamespace

    calls = []

    def request(url, key, payload=None, **kwargs):
        calls.append((url, payload))
        if url.endswith("/health"):
            return {"capabilities": ["capture_idempotency_v1"]}
        if url.endswith("/datasets"):
            assert payload == {"name": "eval_trial"}
            return {"id": "test-dataset"}
        if url.endswith("/remember/entry"):
            assert payload["dataset_id"] == "test-dataset"
            assert payload["dataset_name"] == payload["session_id"] == "eval_trial"
            return {"entry_id": "receipt", "client_event_id": payload["client_event_id"]}
        if url.endswith("/improve"):
            assert payload["dataset_name"] == "eval_trial" and not payload["run_in_background"]
            return {"status": "completed", "stages": [{"status": "completed"}]}
        return completion()

    def recall_child(command, **kwargs):
        assert kwargs["timeout"] == 45
        return SimpleNamespace(
            returncode=0, stdout=json.dumps([{"text": "EVIDENCE:current\nstore=postgres"}])
        )

    monkeypatch.setattr(live_runner, "request_json", request)
    monkeypatch.setattr(live_runner.subprocess, "run", recall_child)
    monkeypatch.setattr(
        live_runner, "server_usage", lambda *args: {"id": "eval_trial", "source": "server"}
    )
    result = live_runner.run(live_request("cognee"))
    assert result["retrieved_ids"] == ["current"]
    assert not result["usage_complete"]  # unknown accounting coverage stays explicit


def test_live_seed_failure_never_becomes_a_quality_result(live_runner, monkeypatch):
    monkeypatch.setattr(live_runner, "request_json", lambda *args, **kw: {"capabilities": []})
    with pytest.raises(ValueError, match="capture_idempotency"):
        live_runner.run(live_request("cognee"))
