"""Matched task trials. No quality claim is inferred from operational telemetry."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from pathlib import Path

RECALL_BUDGET_SECONDS = 45
SCHEMA = 1


def number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    at = (len(values) - 1) * q
    lo = int(at)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (at - lo)


def usage_totals(rows):
    """Count each source-qualified operation once; inclusive parents own child usage."""
    unique = {}
    for row in rows:
        key = (row.get("source"), row.get("id"))
        if key[0] not in {"client", "server"} or not key[1]:
            raise ValueError("usage needs a client/server source and stable operation id")
        if key in unique and unique[key] != row:
            raise ValueError("conflicting usage records for one operation")
        unique[key] = row
    selected = []
    for key, row in unique.items():
        parent = row.get("parent_id")
        seen = {key[1]}
        covered = False
        while parent and (key[0], parent) in unique:
            if parent in seen:
                raise ValueError("usage parent cycle")
            seen.add(parent)
            ancestor = unique[(key[0], parent)]
            covered |= ancestor.get("includes_children") is True
            parent = ancestor.get("parent_id")
        if not covered:
            selected.append(row)
    totals = {}
    for field in ("tokens_in", "tokens_out", "cost_usd"):
        measured = [r[field] for r in selected if number(r.get(field))]
        totals[field] = sum(measured) if len(measured) == len(selected) and selected else None
        totals["measured_" + field] = sum(measured)
    totals["operations"] = len(selected)
    totals["token_bases"] = sorted({r.get("token_basis", "unknown") for r in selected})
    totals["cost_bases"] = sorted({r.get("cost_basis", "unknown") for r in selected})
    totals["sources"] = sorted({r["source"] for r in selected})
    totals["total_tokens"] = (
        totals["tokens_in"] + totals["tokens_out"]
        if totals["tokens_in"] is not None and totals["tokens_out"] is not None
        else None
    )
    return totals


def score(case, response):
    """Use held-out assertions; citations must be retrieved and support the stated claim."""
    expected = case["expected"]
    claims = {}
    for claim in response.get("claims", []):
        if claim["id"] in claims:
            raise ValueError("duplicate answer claim")
        claims[claim["id"]] = claim
    retrieved = set(response.get("retrieved_ids", []))
    correct = citations = supported = false = cited_claims = 0
    for name, claim in claims.items():
        oracle = expected.get(name, {})
        right = name in expected and claim.get("value") == oracle["value"]
        correct += int(right)
        false += int(claim.get("value") in oracle.get("false_values", []))
        refs = claim.get("citations", [])
        citations += len(refs)
        valid = sum(right and ref in oracle.get("sources", []) and ref in retrieved for ref in refs)
        supported += valid
        cited_claims += int(valid > 0)
    return {
        "task_success": correct == len(expected),
        "correct_claims": correct,
        "expected_claims": len(expected),
        "citations": citations,
        "supported_citations": supported,
        "claims_with_supported_citations": cited_claims,
        "false_memory_adoptions": false,
        "scored_claims": len(expected),
    }


def summarize(records, *, include_cases=True):
    """Only complete matched blocks contribute to comparisons; failures stay in denominators."""
    if not records:
        raise ValueError("no trials")
    variants = records[0]["variants"]
    if (
        "no_memory" not in variants
        or "cognee" not in variants
        or len(set(variants)) != len(variants)
    ):
        raise ValueError("include distinct no_memory and cognee variants")
    blocks = defaultdict(dict)
    sessions, datasets = set(), set()
    for row in records:
        if row["variants"] != variants or row["recall_budget_seconds"] != 45:
            raise ValueError("trial budgets or variants differ")
        key = (row["case_id"], row["repetition"])
        if row["variant"] not in variants or row["variant"] in blocks[key]:
            raise ValueError("unknown or duplicate trial variant")
        if row["session_id"] in sessions or row["dataset_name"] in datasets:
            raise ValueError("memory isolation was reused between trials")
        sessions.add(row["session_id"])
        datasets.add(row["dataset_name"])
        blocks[key][row["variant"]] = row
    complete, incomplete = [], []
    for key, block in blocks.items():
        if set(block) != set(variants):
            incomplete.append(list(key))
            continue
        controls = {
            tuple(
                block[v][k] for k in ("model", "fixture_hash", "memory_hash", "seed", "max_tokens")
            )
            for v in variants
        }
        if len(controls) != 1:
            raise ValueError(
                "matched trials must share model, repository, snapshot, seed and token budget"
            )
        complete.append(block)
    groups, paired = {}, {}
    for variant in variants:
        rows = [b[variant] for b in complete]
        n = len(rows)
        citations = sum(r["outcome"]["citations"] for r in rows)
        claims = sum(r["outcome"]["scored_claims"] for r in rows)
        costs = [r["usage"]["cost_usd"] for r in rows]
        tokens = [r["usage"]["total_tokens"] for r in rows]
        groups[variant] = {
            "trials": n,
            "failures": sum(r["status"] != "ok" for r in rows),
            "task_success_rate": sum(r["outcome"]["task_success"] for r in rows) / n if n else None,
            "citation_correctness": sum(r["outcome"]["supported_citations"] for r in rows)
            / citations
            if citations
            else None,
            "citations": citations,
            "citation_coverage": sum(r["outcome"]["claims_with_supported_citations"] for r in rows)
            / claims
            if claims
            else None,
            "false_memory_adoption_rate": sum(r["outcome"]["false_memory_adoptions"] for r in rows)
            / claims
            if claims
            else None,
            "total_tokens": sum(tokens) if n and all(t is not None for t in tokens) else None,
            "total_cost_usd": sum(costs) if n and all(c is not None for c in costs) else None,
            "usage_complete_trials": sum(r["usage_complete"] for r in rows),
            "latency_p50_ms": quantile([r["latency_ms"] for r in rows], 0.5),
            "latency_p95_ms": quantile([r["latency_ms"] for r in rows], 0.95),
            "recall_p50_ms": quantile(
                [r["recall_ms"] for r in rows if r["recall_ms"] is not None], 0.5
            ),
            "recall_p95_ms": quantile(
                [r["recall_ms"] for r in rows if r["recall_ms"] is not None], 0.95
            ),
            "token_bases": sorted({x for r in rows for x in r["usage"]["token_bases"]}),
            "cost_bases": sorted({x for r in rows for x in r["usage"]["cost_bases"]}),
        }
        for baseline in ("no_memory", "cognee"):
            if variant == baseline or (variant == "no_memory" and baseline == "cognee"):
                continue
            differences = [
                int(b[variant]["outcome"]["task_success"])
                - int(b[baseline]["outcome"]["task_success"])
                for b in complete
            ]
            paired[variant + "_vs_" + baseline] = {
                "baseline": baseline,
                "pairs": n,
                "wins": differences.count(1),
                "losses": differences.count(-1),
                "ties": differences.count(0),
                "task_success_delta": sum(differences) / n if n else None,
            }
    repeated = bool(complete) and all(
        sum(block[variants[0]]["case_id"] == case for block in complete) >= 2
        for case in {key[0] for key in blocks}
    )
    return {
        "schema": SCHEMA,
        "kind": "controlled_outcomes",
        "recall_budget_seconds": 45,
        "matched_blocks": len(complete),
        "incomplete_blocks": incomplete,
        "repeated": repeated,
        "variants": groups,
        "paired": paired,
        "by_case": {
            case: summarize([r for r in records if r["case_id"] == case], include_cases=False)[
                "variants"
            ]
            for case in sorted({r["case_id"] for r in records})
        }
        if include_cases
        else {},
        "note": (
            "Task assertions and citation oracles are fixture-defined. Smoke runners validate "
            "the harness, not model quality. Null cost/tokens mean unmeasured; reported/estimated "
            "usage is not an invoice. Small samples are descriptive, not evidence of "
            "statistical significance."
        ),
    }


def server_usage(base_url, api_key, session_id):
    """Use one unique session aggregate, never add its child pipeline usage again."""
    url = base_url.rstrip("/") + "/api/v1/sessions/" + urllib.parse.quote(session_id, safe="")
    req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    with urllib.request.urlopen(req, timeout=15) as response:
        data = json.load(response)
    if data.get("session_id") != session_id:
        raise ValueError("server returned another session")
    return {
        "id": session_id,
        "source": "server",
        "includes_children": True,
        "tokens_in": data.get("tokens_in"),
        "tokens_out": data.get("tokens_out"),
        "cost_usd": data.get("cost_usd"),
        "token_basis": "server_reported",
        "cost_basis": "server_estimated",
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def run_trials(
    suite,
    command,
    out_dir,
    *,
    repetitions=3,
    seed=42,
    timeout=300,
    server_url="",
    before_trial=None,
    on_trial=None,
):
    if repetitions < 2 or timeout <= 0:
        raise ValueError("use at least two repetitions and a positive task timeout")
    variants = suite["variants"]
    if (
        "no_memory" not in variants
        or "cognee" not in variants
        or len(set(variants)) != len(variants)
    ):
        raise ValueError("include distinct no_memory and cognee variants")
    if not suite["cases"] or len({c["id"] for c in suite["cases"]}) != len(suite["cases"]):
        raise ValueError("cases must be nonempty and have unique IDs")
    if (
        not suite.get("model")
        or not isinstance(suite.get("max_tokens"), int)
        or suite["max_tokens"] <= 0
    ):
        raise ValueError("pin model and positive max_tokens")
    repository = suite.get("repository")
    revision = None
    if repository:
        source = Path(repository["path"]).resolve()
        if not source.is_dir():
            raise ValueError("repository must be a local checkout")
        revision = subprocess.check_output(
            [
                "git",
                "-C",
                str(source),
                "rev-parse",
                "--verify",
                repository["revision"] + "^{commit}",
            ],
            text=True,
        ).strip()
    out_dir.mkdir(parents=True, exist_ok=False)
    records = []
    rng = random.Random(seed)
    for case in suite["cases"]:
        if not case.get("expected"):
            raise ValueError("each task needs held-out expected claims")
        for repetition in range(repetitions):
            order = list(variants)
            rng.shuffle(order)
            for variant in order:
                if before_trial is not None:
                    before_trial(case, repetition, variant)
                trial_id = uuid.uuid4().hex
                trial_dir = out_dir / trial_id
                workspace = trial_dir / "workspace"
                workspace.mkdir(parents=True)
                if repository:
                    subprocess.run(
                        [
                            "git",
                            "clone",
                            "--quiet",
                            "--shared",
                            "--no-checkout",
                            "--",
                            str(source),
                            str(workspace),
                        ],
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        ["git", "-C", str(workspace), "checkout", "--quiet", "--detach", revision],
                        check=True,
                        capture_output=True,
                    )
                for name, content in case.get("files", {}).items():
                    relative = Path(name)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("fixture paths must stay inside the workspace")
                    path = workspace / relative
                    if workspace.resolve() not in path.resolve().parents:
                        raise ValueError("fixture overlay follows a symlink outside its workspace")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                trial_seed = seed + repetition
                request = {
                    "schema": SCHEMA,
                    "trial_id": trial_id,
                    "variant": variant,
                    "model": suite["model"],
                    "max_tokens": suite["max_tokens"],
                    "seed": trial_seed,
                    "session_id": "eval_" + trial_id,
                    "dataset_name": "eval_" + trial_id,
                    "recall_budget_seconds": 45,
                    "workspace": str(workspace.resolve()),
                    "state_dir": str((trial_dir / "state").resolve()),
                    "task": {"id": case["id"], "prompt": case["prompt"]},
                    "memory_seed": [] if variant == "no_memory" else case.get("memory", []),
                    "variant_config": suite.get("variant_config", {}).get(variant, {}),
                }
                env = dict(
                    os.environ,
                    COGNEE_RECALL_BUDGET="45",
                    COGNEE_PLUGIN_DATASET=request["dataset_name"],
                    COGNEE_SESSION_ID=request["session_id"],
                    COGNEE_CAPTURE="false",
                )
                response, status = {}, "ok"
                started = time.monotonic()
                try:
                    process = subprocess.run(
                        command,
                        input=json.dumps(request),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        cwd=workspace,
                        env=env,
                        timeout=timeout,
                    )
                    if process.returncode:
                        raise ValueError("runner failed")
                    response = json.loads(process.stdout)
                    if not isinstance(response, dict):
                        response = {}
                        raise ValueError("runner response must be an object")
                    if response.get("model") != suite["model"]:
                        raise ValueError("runner changed the pinned model")
                    if (
                        response.get("session_id") != request["session_id"]
                        or response.get("dataset_name") != request["dataset_name"]
                    ):
                        raise ValueError("runner did not confirm isolated memory")
                    if not number(response.get("recall_ms")) or response["recall_ms"] > 45000:
                        raise ValueError("runner exceeded the recall deadline or omitted latency")
                    if variant == "no_memory" and response.get("retrieved_ids"):
                        raise ValueError("no-memory trial accessed memory")
                    if not set(response.get("retrieved_ids", [])).issubset(
                        {m["id"] for m in case.get("memory", [])}
                    ):
                        raise ValueError("retrieved IDs escape this trial snapshot")
                    outcome = score(case, response)
                    if case.get("verify"):
                        remaining = max(0.001, timeout - (time.monotonic() - started))
                        check = subprocess.run(
                            case["verify"],
                            cwd=workspace,
                            env=env,
                            capture_output=True,
                            timeout=remaining,
                        )
                        outcome["task_success"] &= check.returncode == 0
                except subprocess.TimeoutExpired:
                    status = "timeout"
                except (ValueError, KeyError, TypeError, OSError):
                    status = "invalid_or_failed"
                latency_ms = (time.monotonic() - started) * 1000
                if status != "ok":
                    outcome = score(case, {})
                    response["retrieved_ids"] = []
                rows = response.get("usage", []) if isinstance(response, dict) else []
                if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                    rows = []
                if server_url and variant != "no_memory":
                    try:
                        # Server aggregate replaces all runner-provided server rows.
                        rows = [r for r in rows if r.get("source") != "server"]
                        rows.append(
                            server_usage(
                                server_url,
                                os.environ.get("COGNEE_API_KEY", ""),
                                request["session_id"],
                            )
                        )
                    except (OSError, ValueError):
                        rows.append({"id": request["session_id"], "source": "server"})
                try:
                    usage = usage_totals(rows)
                except (ValueError, TypeError, AttributeError, KeyError):
                    usage = usage_totals([])
                complete = response.get("usage_complete") is True and "client" in usage["sources"]
                complete &= variant == "no_memory" or "server" in usage["sources"]
                complete &= usage["total_tokens"] is not None and usage["cost_usd"] is not None
                if not complete:
                    usage["total_tokens"] = usage["cost_usd"] = None
                record = {
                    "schema": SCHEMA,
                    "case_id": case["id"],
                    "repetition": repetition,
                    "variant": variant,
                    "variants": variants,
                    "model": suite["model"],
                    "seed": trial_seed,
                    "max_tokens": suite["max_tokens"],
                    "session_id": request["session_id"],
                    "dataset_name": request["dataset_name"],
                    "fixture_hash": digest(
                        {
                            "prompt": case["prompt"],
                            "files": case.get("files", {}),
                            "repository_revision": revision,
                        }
                    ),
                    "memory_hash": digest(case.get("memory", [])),
                    "recall_budget_seconds": 45,
                    "status": status,
                    "outcome": outcome,
                    "latency_ms": latency_ms,
                    "recall_ms": response.get("recall_ms")
                    if number(response.get("recall_ms"))
                    else None,
                    "retrieved_items": len(response.get("retrieved_ids", [])),
                    "usage": usage,
                    "usage_complete": bool(complete),
                }
                if on_trial is not None:
                    on_trial(record, request, response)
                records.append(record)
                with (out_dir / "trials.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, allow_nan=False) + "\n")
                # Keep aggregate records; remove isolated task content and runner caches.
                shutil.rmtree(trial_dir)
    report = summarize(records)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--suite", type=Path, required=True)
    run.add_argument(
        "--runner", required=True, help="JSON argv array; reads one JSON request on stdin"
    )
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--repetitions", type=int, default=3)
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--timeout", type=float, default=300)
    run.add_argument("--server-url", default="")
    report = sub.add_parser("report")
    report.add_argument("--trials", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            result = summarize(
                [json.loads(line) for line in args.trials.read_text().splitlines() if line.strip()]
            )
        else:
            command = json.loads(args.runner)
            if (
                not isinstance(command, list)
                or not command
                or not all(isinstance(x, str) for x in command)
            ):
                raise ValueError("runner must be a nonempty argv array")
            result = run_trials(
                json.loads(args.suite.read_text()),
                command,
                args.out.resolve(),
                repetitions=args.repetitions,
                seed=args.seed,
                timeout=args.timeout,
                server_url=args.server_url,
            )
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        print("outcome_eval: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
