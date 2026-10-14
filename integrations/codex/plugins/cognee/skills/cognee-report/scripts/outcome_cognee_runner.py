"""Live Cognee + OpenAI-compatible model adapter for matched outcome trials.

Requires explicit COGNEE_BASE_URL/COGNEE_API_KEY and EVAL_MODEL_URL/EVAL_MODEL_API_KEY.
EVAL_MODEL_URL is the complete chat-completions URL. Running this adapter incurs
normal model/server costs and creates isolated eval_* datasets; it never uses the
user's active plugin dataset. Use outcome_smoke_runner.py for offline validation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from outcome_eval import server_usage


def request_json(url, key, payload=None, *, timeout=120, bearer=False):
    headers = {"Content-Type": "application/json"}
    headers["Authorization" if bearer else "X-Api-Key"] = "Bearer " + key if bearer else key
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def recall(request, url, key):
    config = request.get("variant_config", {})
    return request_json(
        url + "/api/v1/recall",
        key,
        {
            "query": request["task"]["prompt"],
            "datasets": [request["dataset_name"]],
            "session_id": request["session_id"],
            "only_context": True,
            "scope": config.get("scope", ["graph"]),
            "top_k": config.get("top_k", 5),
        },
        timeout=45,
    )


def run(request):
    model_url, model_key = os.environ["EVAL_MODEL_URL"], os.environ["EVAL_MODEL_API_KEY"]
    url, key = (
        os.environ.get("COGNEE_BASE_URL", "").rstrip("/"),
        os.environ.get("COGNEE_API_KEY", ""),
    )
    memory, recalled, recall_ms, server = "", [], 0.0, []
    if request["variant"] != "no_memory":
        if not url or not key:
            raise ValueError("Set explicit Cognee credentials for isolated live trials")
        health = request_json(url + "/health", key)
        if "capture_idempotency_v1" not in health.get("capabilities", []):
            raise ValueError("The evaluation server needs capture_idempotency_v1")
        for evidence in request["memory_seed"]:
            event_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, request["session_id"] + ":" + evidence["id"])
            )
            payload = {
                "dataset_name": request["dataset_name"],
                "session_id": request["session_id"],
                "client_event_id": event_id,
                "entry": {
                    "type": "qa",
                    "question": "Saved evidence",
                    "answer": "EVIDENCE:" + evidence["id"] + "\n" + evidence["text"],
                },
            }
            receipt = request_json(url + "/api/v1/remember/entry", key, payload)
            if receipt.get("client_event_id") != event_id:
                raise ValueError("seed capture did not return the event receipt")
        improved = request_json(
            url + "/api/v1/improve",
            key,
            {
                "dataset_name": request["dataset_name"],
                "session_ids": [request["session_id"]],
                "run_in_background": False,
            },
            timeout=180,
        )
        if (
            not isinstance(improved, dict)
            or improved.get("status") not in {"completed", "skipped"}
            or not improved.get("stages")
            or any(stage.get("status") == "errored" for stage in improved.get("stages", []))
        ):
            raise ValueError("fixture memory preparation failed")
        started = time.monotonic()
        # A child process gives recall a real elapsed deadline, including a stalled
        # socket or slow response stream. Late context is never handed to the model.
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--recall-only"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=45,
        )
        recall_ms = (time.monotonic() - started) * 1000
        if child.returncode or recall_ms > 45000:
            raise ValueError("recall failed or exceeded 45 seconds")
        rows = json.loads(child.stdout)
        if not isinstance(rows, list):
            raise ValueError("recall did not return results")
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        from _injection_budget import fit_sections
        from _recall_text import trim_recall_text

        sections = []
        for row in rows:
            text = str(row.get("text") or row.get("content") or "")
            trimmed, _, _ = trim_recall_text(text, request["task"]["prompt"], request["session_id"])
            sections.append({"entries": [trimmed], "priority": 40})
        memory, _ = fit_sections(sections, 9000)
        recalled = [e["id"] for e in request["memory_seed"] if "EVIDENCE:" + e["id"] in memory]
    prompt = (
        "Complete the task using relevant evidence. "
        "Superseded or irrelevant memory is not authority. "
        'Return JSON only: {"claims":[{"id":"requested claim id","value":"answer",'
        '"citations":["evidence id"]}]}. Cite only evidence actually supplied.\nTask:\n'
        + request["task"]["prompt"]
        + "\nMemory:\n"
        + memory
    )
    completion = request_json(
        model_url,
        model_key,
        {
            "model": request["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": request["max_tokens"],
            "temperature": 0,
            "seed": request["seed"],
            "response_format": {"type": "json_object"},
        },
        bearer=True,
    )
    answer = json.loads(completion["choices"][0]["message"]["content"])
    usage = completion.get("usage") or {}
    cost = usage.get("cost")
    cost_basis = "provider_reported" if cost is not None else "unknown"
    if (
        cost is None
        and os.environ.get("EVAL_INPUT_USD_PER_MILLION")
        and os.environ.get("EVAL_OUTPUT_USD_PER_MILLION")
        and usage.get("prompt_tokens") is not None
        and usage.get("completion_tokens") is not None
    ):
        cost = (
            usage["prompt_tokens"] * float(os.environ["EVAL_INPUT_USD_PER_MILLION"])
            + usage["completion_tokens"] * float(os.environ["EVAL_OUTPUT_USD_PER_MILLION"])
        ) / 1_000_000
        cost_basis = "configured_rates"
    client = {
        "source": "client",
        "id": completion.get("id") or request["trial_id"],
        "tokens_in": usage.get("prompt_tokens"),
        "tokens_out": usage.get("completion_tokens"),
        "cost_usd": cost,
        "token_basis": "provider_reported",
        "cost_basis": cost_basis,
    }
    if request["variant"] != "no_memory":
        try:
            server = [server_usage(url, key, request["session_id"])]
        except (OSError, ValueError):
            server = [{"source": "server", "id": request["session_id"]}]
    return {
        "model": request["model"],
        "session_id": request["session_id"],
        "dataset_name": request["dataset_name"],
        "claims": answer.get("claims", []),
        "retrieved_ids": recalled,
        "recall_ms": recall_ms,
        "usage": [client, *server],
        # Existing server aggregates mix measured and estimated tokens/prices.
        # Opt in only after checking coverage for the configured provider paths.
        "usage_complete": request["variant"] == "no_memory"
        or os.environ.get("EVAL_SERVER_USAGE_COMPLETE") == "true",
    }


def main():
    request = json.load(sys.stdin)
    if sys.argv[1:] == ["--recall-only"]:
        result = recall(
            request, os.environ["COGNEE_BASE_URL"].rstrip("/"), os.environ["COGNEE_API_KEY"]
        )
    else:
        result = run(request)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
