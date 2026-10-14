import json
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import quality_job as job  # noqa: E402


@pytest.fixture
def warehouse():
    con = duckdb.connect(":memory:")
    yield job.Warehouse(con)
    con.close()


def test_immutable_warehouse_retries(warehouse):
    warehouse.put("eval_cases", "id", "run", {"x": 1})
    warehouse.put("eval_cases", "id", "run", {"x": 1})
    assert warehouse.con.execute("select count(*) from quality_eval.eval_cases").fetchone() == (1,)
    with pytest.raises(ValueError, match="conflicts"):
        warehouse.put("eval_cases", "id", "run", {"x": 2})
    with pytest.raises(ValueError):
        job.Warehouse(warehouse.con, "invalid; DROP TABLE eval_cases")


def test_budget_reserves_before_work_and_rejects_unknown_usage():
    limits = json.loads((ROOT / "limits.example.json").read_text())
    limits["max_trials"] = 1
    budget = job.Budget(limits)
    budget.reserve("trial")
    with pytest.raises(job.BudgetExceeded):
        budget.reserve("trial")
    with pytest.raises(job.BudgetExceeded, match="incomplete"):
        budget.verify_usage("trial", None, 0)
    with pytest.raises(job.BudgetExceeded, match="exceeded"):
        budget.verify_usage("trial", limits["trial_tokens"] + 1, 0)


def test_offline_pipeline_stores_paired_traces_without_product_claim(warehouse, tmp_path):
    suite = json.loads((ROOT / "smoke-suite.json").read_text())
    result = job.execute(
        suite,
        [sys.executable, str(job.SCRIPTS / "outcome_smoke_runner.py")],
        tmp_path / "run",
        warehouse,
        json.loads((ROOT / "limits.example.json").read_text()),
    )
    assert result["pairs"] == 14
    assert result["status"] == "pending" and result["source"] == "smoke"
    assert result["recall_budget_seconds"] == 45
    assert warehouse.con.execute("select count(*) from quality_eval.eval_trials").fetchone() == (
        28,
    )
    row = warehouse.con.execute("select payload from quality_eval.eval_trials limit 1").fetchone()
    trace = json.loads(row[0])
    assert "claims" in trace["response"] and "expected" not in trace["request"]["task"]
    assert (tmp_path / "run/quality-summary.json").exists()


def test_checks_outrank_judge():
    good = {"outcome": {"task_success": True, "false_memory_adoptions": 0}}
    bad = {"outcome": {"task_success": False, "false_memory_adoptions": 1}}
    assert job.deterministic_verdict(good, bad) == "worse"
    assert job.deterministic_verdict(bad, good) == "better"
    assert job.deterministic_verdict(bad, bad) == "equivalent"
    assert job.deterministic_verdict(good, good) is None


def test_jev_order_mapping_and_no_variant_names(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
    seen = set()

    def call(url, key, payload):
        assert key == "test-only"
        assert set(payload["state"]) == {"task", "reference_checks", "reference_evidence", "A", "B"}
        selected = "A" if payload["state"]["A"] == "correct" else "B"
        seen.add(selected)
        return {"model": "jev-pinned", "answers": {"comparison": {"choice": selected}}}

    for seed in range(10):
        result, _ = job.judge_pair(
            {"prompt": "task", "expected": {}}, "wrong", "correct", "jev-pinned", seed, call=call
        )
        assert result == "better"
    assert seen == {"A", "B"}


def test_summary_rate_includes_ties_and_losses():
    verdicts = [{"verdict": x} for x in ["better"] * 12 + ["equivalent"] * 6 + ["worse"] * 2]
    summary = job.build_summary(
        "run", {"model": "model", "cases": [1, 2]}, verdicts, {}, source="live"
    )
    assert summary["improvement_rate"] == 0.6 and summary["regression_rate"] == 0.1
    assert summary["status"] == "ready"
    verdicts[-1] = {"verdict": "unjudgeable"}
    assert (
        job.build_summary("run", {"model": "model", "cases": [1]}, verdicts, {}, source="live")[
            "status"
        ]
        == "pending"
    )


def test_live_adapter_creates_dataset_before_idempotent_capture(monkeypatch):
    import types

    import outcome_cognee_runner as adapter

    monkeypatch.setenv("EVAL_MODEL_URL", "https://model.test/completions")
    monkeypatch.setenv("EVAL_MODEL_API_KEY", "test")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://eval.test")
    monkeypatch.setenv("COGNEE_API_KEY", "test")
    calls = []

    def request(url, key, payload=None, **kwargs):
        calls.append((url, payload))
        if url.endswith("/health"):
            return {"capabilities": ["capture_idempotency_v1"]}
        if url.endswith("/datasets"):
            return {"id": "dataset-id"}
        if url.endswith("/remember/entry"):
            assert payload["dataset_id"] == "dataset-id"
            assert calls[-2][0].endswith("/datasets")
            return {"client_event_id": payload["client_event_id"]}
        if url.endswith("/improve"):
            return {"status": "completed", "stages": [{"status": "completed"}]}
        return {
            "model": "pinned",
            "choices": [{"message": {"content": '{"claims":[]}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "cost": 0.001},
        }

    monkeypatch.setattr(adapter, "request_json", request)
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *a, **kw: types.SimpleNamespace(
            returncode=0, stdout='[{"text":"EVIDENCE:fact\\nproject evidence"}]'
        ),
    )
    monkeypatch.setattr(
        adapter,
        "server_usage",
        lambda *a: {
            "id": "session",
            "source": "server",
            "tokens_in": 10,
            "tokens_out": 2,
            "cost_usd": 0.001,
        },
    )
    response = adapter.run(
        {
            "variant": "cognee",
            "dataset_name": "eval_test",
            "session_id": "eval_test",
            "memory_seed": [{"id": "fact", "text": "project evidence"}],
            "task": {"prompt": "question"},
            "model": "pinned",
            "seed": 1,
            "max_tokens": 50,
            "trial_id": "trial",
        }
    )
    assert response["answer"] == '{"claims":[]}'
    assert "injected_context" in response


def test_recent_matching_summary_cache(warehouse):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    summary = {
        "evaluation_key": "key",
        "status": "ready",
        "source": "live",
        "evaluated_at": now.isoformat(),
    }
    warehouse.put("eval_summaries", "run", "run", summary)
    assert warehouse.cached_summary("key", now) == summary
    assert warehouse.cached_summary("other-key", now) is None
    assert warehouse.cached_summary("key", now + timedelta(days=1)) is None
