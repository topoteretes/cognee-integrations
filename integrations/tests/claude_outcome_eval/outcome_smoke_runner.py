"""Deterministic protocol smoke fixture. This is NOT a model-quality benchmark.

Reads facts from the supplied task/memory only, never the held-out scoring oracle.
"""

import json
import sys
import time


def main():
    request = json.load(sys.stdin)
    start = time.monotonic()
    facts = {}
    for record in request["memory_seed"]:
        # The fixture's explicit key=value facts simulate a retrieval/answer adapter.
        for line in record["text"].splitlines():
            key, separator, value = line.partition("=")
            if separator:
                facts[key] = {"id": key, "value": value, "citations": [record["id"]]}
    claims = list(facts.values())
    recalled = [m["id"] for m in request["memory_seed"]]
    elapsed = (time.monotonic() - start) * 1000
    usage = [
        {
            "id": "answer",
            "source": "client",
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0,
            "token_basis": "no_model_smoke",
            "cost_basis": "no_model_smoke",
        }
    ]
    if request["variant"] != "no_memory":
        usage.append({**usage[0], "id": "retrieval", "source": "server"})
    print(
        json.dumps(
            {
                "model": request["model"],
                "session_id": request["session_id"],
                "dataset_name": request["dataset_name"],
                "claims": claims,
                "retrieved_ids": recalled,
                "recall_ms": elapsed,
                "usage": usage,
                "usage_complete": True,
            }
        )
    )


if __name__ == "__main__":
    main()
