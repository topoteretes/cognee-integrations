"""Three-condition comparison harness: no memory / saved summary / Cognee retrieval.

THIS IS A HARNESS, NOT RESULTS. It wires the adapter into a repeatable loop over
task variations so a real evaluation (real agent, real LLM, isolated datasets,
matched settings, several repetitions) can be run and reported with cold-start
and subsequent runs kept apart. By default it runs with a fake agent so the
plumbing can be exercised without a browser or an LLM:

    uv run python evals/run_comparison.py --fake --repetitions 2

Measured per run: correctness against the fixture's expected answer, steps,
wall time, whether recalled evidence was present, and the memory status. Costs
(LLM tokens, graph extraction) must be read from your provider and cognee, not
from here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from cognee_integration_browser_use import CogneeBrowserMemory, MemoryScope

FIXTURES = Path(__file__).parent / "fixtures"


class FakeHistory:
    def __init__(self, answer: str, steps: int):
        self._answer, self._steps = answer, steps

    def final_result(self):
        return self._answer

    def is_done(self):
        return True

    def is_successful(self):
        return True

    def number_of_steps(self):
        return self._steps

    def urls(self):
        return ["https://fixture.example/page"]


class FakeAgent:
    """Answers from the fixture; takes fewer 'steps' when evidence was injected."""

    def __init__(self, case: dict[str, Any], evidence_present: bool):
        self.case = case
        self.history = FakeHistory(
            case["expected"], 2 if evidence_present else case.get("baseline_steps", 6)
        )

    async def run(self, max_steps=100, on_step_start=None, on_step_end=None):
        for _ in range(self.history.number_of_steps()):
            if on_step_end:
                await on_step_end(self)
        return self.history


async def run_condition(
    condition: str,
    case: dict[str, Any],
    scope: MemoryScope,
    saved_summary: dict[str, str],
    fake: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    evidence_present = False
    if condition == "cognee":
        async with CogneeBrowserMemory() as memory:
            run = memory.new_run(task=case["task"], scope=scope, query_hints=case.get("hints", []))
            context = await run.recall_context()
            evidence_present = context.status == "ok"
            agent = FakeAgent(case, evidence_present) if fake else _real_agent(case, run, context)
            history = await run.execute(agent, max_steps=case.get("max_steps", 30))
            status = run.status.to_dict()
    elif condition == "saved_summary":
        evidence_present = case["id"] in saved_summary
        agent = (
            FakeAgent(case, evidence_present)
            if fake
            else _real_agent(case, None, None, saved_summary.get(case["id"]))
        )
        history = await agent.run(max_steps=case.get("max_steps", 30))
        saved_summary[case["id"]] = str(history.final_result())
        status = {"condition": "saved_summary"}
    else:
        agent = FakeAgent(case, False) if fake else _real_agent(case, None, None)
        history = await agent.run(max_steps=case.get("max_steps", 30))
        status = {"condition": "no_memory"}
    answer = str(history.final_result() or "")
    return {
        "condition": condition,
        "case": case["id"],
        "correct": case["expected"].lower() in answer.lower(),
        "steps": history.number_of_steps(),
        "wall_s": round(time.perf_counter() - started, 3),
        "evidence_present": evidence_present,
        "memory": status,
    }


def _real_agent(case, run, context, saved_summary=None):
    from browser_use import Agent, ChatOpenAI, Tools

    tools = Tools()
    extension = None
    if run is not None:
        run.register_tools(tools)
        extension = run.instructions(context)
    elif saved_summary:
        extension = f"Notes from a previous run (may be stale):\n{saved_summary}"
    return Agent(
        task=case["task"],
        llm=ChatOpenAI(model="gpt-4.1-mini"),
        tools=tools,
        extend_system_message=extension,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--fake", action="store_true", help="use the fake agent (no browser, no LLM)"
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--tenant", default=f"eval-{int(time.time())}")
    parser.add_argument("--out", default="evals/results/latest.jsonl")
    args = parser.parse_args()
    if not os.environ.get("COGNEE_API_KEY"):
        print(
            "The 'cognee' condition writes to and reads from a real Cognee server even with "
            "--fake: set COGNEE_BASE_URL and COGNEE_API_KEY (a disposable server is fine).",
            file=sys.stderr,
        )
        sys.exit(2)

    cases = json.loads((FIXTURES / "supplier_tasks.json").read_text())
    scope = MemoryScope(tenant_id=args.tenant, project_id="eval")
    saved_summary: dict[str, str] = {}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for repetition in range(args.repetitions):
        for case in cases:
            for condition in ("no_memory", "saved_summary", "cognee"):
                row = await run_condition(condition, case, scope, saved_summary, args.fake)
                row["repetition"] = repetition
                rows.append(row)
                print(json.dumps(row, default=str))
    out.write_text("\n".join(json.dumps(r, default=str) for r in rows) + "\n")
    print(f"\nwrote {len(rows)} rows to {out} (harness output; not a published result)")


if __name__ == "__main__":
    asyncio.run(main())
