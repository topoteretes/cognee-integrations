from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import time
import warnings
from pathlib import Path
from typing import Any
from uuid import UUID


ROOT = Path(__file__).resolve().parent
SKILLS_ROOT = ROOT / "my_skills"
SKILL_NAME = os.environ.get("COGNEE_SKILL_NAME", "code-review")
DATASET_NAME = os.environ.get("COGNEE_SKILL_DATASET", "n8n-skill-self-improvement")
SESSION_ID = os.environ.get("COGNEE_SKILL_SESSION", "n8n-skill-self-improvement-session")
STATE_FILE = Path(
    os.environ.get(
        "COGNEE_SELF_IMPROVE_STATE",
        str(ROOT / ".n8n_state" / "skill_self_improve_state.json"),
    )
).expanduser()
COGNEE_REPO = (
    Path(os.environ["COGNEE_REPO"]).expanduser().resolve()
    if os.environ.get("COGNEE_REPO")
    else None
)
SYSTEM_ROOT_DIRECTORY = (
    Path(
        os.environ.get("SYSTEM_ROOT_DIRECTORY")
        or os.environ.get("COGNEE_SELF_IMPROVE_SYSTEM_ROOT")
        or ROOT / ".cognee_system"
    )
    .expanduser()
    .resolve()
)
DATA_ROOT_DIRECTORY = (
    Path(
        os.environ.get("DATA_ROOT_DIRECTORY")
        or os.environ.get("COGNEE_SELF_IMPROVE_DATA_ROOT")
        or ROOT / ".cognee_data"
    )
    .expanduser()
    .resolve()
)

os.environ.setdefault("LOG_LEVEL", "ERROR")
os.environ.setdefault("COGNEE_LOG_FILE", "false")
os.environ.setdefault("COGNEE_CLI_MODE", "true")
os.environ.setdefault("SYSTEM_ROOT_DIRECTORY", str(SYSTEM_ROOT_DIRECTORY))
os.environ.setdefault("DATA_ROOT_DIRECTORY", str(DATA_ROOT_DIRECTORY))
os.environ["COGNEE_SKILL_SOURCE_ROOTS"] = os.pathsep.join(
    str(path)
    for path in [
        ROOT,
        SKILLS_ROOT,
        *(os.environ.get("COGNEE_SKILL_SOURCE_ROOTS", "").split(os.pathsep)),
    ]
    if str(path)
)

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    if COGNEE_REPO is not None:
        load_dotenv(COGNEE_REPO / ".env")
except Exception:
    pass

warnings.filterwarnings("ignore", message="This declarative base already contains a class.*")

import cognee
from cognee import SearchType
from cognee.context_global_variables import set_database_global_context_variables
from cognee.memory import SkillRunEntry
from cognee.modules.engine.operations.setup import setup
from cognee.modules.memify.skill_improvement import improve_skill
from cognee.modules.pipelines.layers.resolve_authorized_user_datasets import (
    resolve_authorized_user_datasets,
)
from cognee.modules.tools.resolve_skills import find_skill_by_name

cognee.config.data_root_directory(str(DATA_ROOT_DIRECTORY))
cognee.config.system_root_directory(str(SYSTEM_ROOT_DIRECTORY))

DEFAULT_TASK_TEXT = """Review this auth boundary diff. Return only JSON with keys skill_to_improve,
score, result_summary, and missing_instruction.

Diff:
diff --git a/auth/session.py b/auth/session.py
@@
 def dataset_for_user(user, requested_dataset):
-    return requested_dataset
+    return get_dataset(requested_dataset)

Expected review focus: identify that the code does not verify the requested
dataset belongs to the authenticated user before returning it.
"""
TASK_TEXT = os.environ.get("COGNEE_SKILL_TASK", DEFAULT_TASK_TEXT)


def truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def truthy_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def skill_file_for(skill_name: str) -> Path:
    return SKILLS_ROOT / skill_name / "SKILL.md"


def json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str))


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")


def unwrap_answer(answer: Any) -> Any:
    if isinstance(answer, list) and answer:
        return unwrap_answer(answer[0])
    if isinstance(answer, dict) and "search_result" in answer:
        return unwrap_answer(answer["search_result"])
    return answer


def parse_json_answer(answer: Any) -> dict[str, Any]:
    text = unwrap_answer(answer)
    if not isinstance(text, str):
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match is None:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


def normalize_score(value: Any, default: float = 0.3) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = default
    if 1.0 < score <= 10.0:
        score = score / 10.0
    elif 10.0 < score <= 100.0:
        score = score / 100.0
    return max(0.0, min(1.0, score))


def result_items(result: Any) -> list[Any]:
    items = getattr(result, "items", [])
    if items is None:
        return []
    if isinstance(items, list):
        return items
    if isinstance(items, tuple):
        return list(items)
    return []


def find_proposal_id(result: Any) -> str | None:
    for item in result_items(result):
        if isinstance(item, dict) and item.get("kind") == "skill_improvement_proposal":
            return item.get("proposal_id")
    return None


def one_line(text: str, limit: int = 500) -> str:
    normalized = " ".join((text or "").split())
    return normalized[:limit]


def unified_skill_diff(before: str, after: str, skill_name: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{skill_name}/SKILL.md.before",
            tofile=f"{skill_name}/SKILL.md.after",
        )
    )


def split_frontmatter(markdown: str) -> tuple[str, str]:
    if not markdown.startswith("---\n"):
        return "", markdown
    end = markdown.find("\n---", 4)
    if end == -1:
        return "", markdown
    close_end = markdown.find("\n", end + 4)
    if close_end == -1:
        close_end = len(markdown)
    return markdown[: close_end + 1], markdown[close_end + 1 :].lstrip()


def current_config_state() -> dict[str, Any]:
    score_threshold = normalize_score(
        os.environ.get("COGNEE_SKILL_SCORE_THRESHOLD", "0.9"), default=0.9
    )
    eval_score = normalize_score(os.environ.get("COGNEE_SKILL_SCORE", "0.3"))
    skill_name = os.environ.get("COGNEE_SKILL_NAME", SKILL_NAME)
    return {
        "run_id": os.environ.get("COGNEE_SELF_IMPROVE_RUN_ID", str(int(time.time() * 1000))),
        "status": "initialized",
        "workflow_root": str(ROOT),
        "skills_root": str(SKILLS_ROOT),
        "skill_name": skill_name,
        "skill_file": str(skill_file_for(skill_name)),
        "dataset": DATASET_NAME,
        "session_id": SESSION_ID,
        "task_text": TASK_TEXT,
        "eval_score": eval_score,
        "score_threshold": score_threshold,
        "score_from_agent": truthy("COGNEE_SKILL_SCORE_FROM_AGENT"),
        "approved": truthy("COGNEE_SELF_IMPROVE_APPROVED", "1"),
        "apply": truthy("COGNEE_SELF_IMPROVE_APPLY", "1"),
        "sync_file": truthy("COGNEE_SELF_IMPROVE_SYNC_FILE", "1"),
        "cognee_repo": str(COGNEE_REPO) if COGNEE_REPO is not None else None,
        "system_root_directory": str(SYSTEM_ROOT_DIRECTORY),
        "data_root_directory": str(DATA_ROOT_DIRECTORY),
        "state_file": str(STATE_FILE),
    }


async def skill_body(skill_name: str, dataset, user) -> str:
    owner_id = getattr(dataset, "owner_id", None) or getattr(user, "id", None)
    if owner_id is None:
        raise ValueError("Skill lookup requires a dataset owner or user id.")
    async with set_database_global_context_variables(dataset.id, owner_id):
        skill = await find_skill_by_name(skill_name, dataset_id=dataset.id)
    if skill is None:
        raise ValueError(f"Skill {skill_name!r} was not found in dataset {dataset.name!r}.")
    return skill.procedure.strip()


async def resolve_dataset_from_state(state: dict[str, Any]):
    dataset_id = state.get("dataset_id")
    if not dataset_id:
        raise ValueError("State does not contain dataset_id. Run remember-skills first.")
    user, datasets = await resolve_authorized_user_datasets(UUID(dataset_id))
    return user, datasets[0]


async def action_dry_run(emit: bool = True) -> dict[str, Any]:
    payload = {
        "status": "dry_run_ok",
        "workflow_root": str(ROOT),
        "skills_root": str(SKILLS_ROOT),
        "skill_file": str(skill_file_for(SKILL_NAME)),
        "cognee_repo": str(COGNEE_REPO) if COGNEE_REPO is not None else None,
        "system_root_directory": str(SYSTEM_ROOT_DIRECTORY),
        "data_root_directory": str(DATA_ROOT_DIRECTORY),
        "state_file": str(STATE_FILE),
    }
    if emit:
        emit_json(payload)
    return payload


async def action_init_state(emit: bool = True) -> dict[str, Any]:
    state = current_config_state()
    save_state(state)
    payload = {
        "status": "state_initialized",
        "run_id": state["run_id"],
        "skill_name": state["skill_name"],
        "dataset": state["dataset"],
        "session_id": state["session_id"],
        "eval_score": state["eval_score"],
        "score_threshold": state["score_threshold"],
        "approved": state["approved"],
        "apply": state["apply"],
        "sync_file": state["sync_file"],
        "state_file": state["state_file"],
    }
    if emit:
        emit_json(payload)
    return payload


async def action_smoke(emit: bool = True) -> dict[str, Any]:
    await setup()
    remembered = await cognee.remember(
        str(SKILLS_ROOT),
        dataset_name=f"{DATASET_NAME}-smoke",
        content_type="skills",
    )
    payload = {
        "status": "smoke_ok",
        "dataset_id": getattr(remembered, "dataset_id", None),
        "system_root_directory": str(SYSTEM_ROOT_DIRECTORY),
        "data_root_directory": str(DATA_ROOT_DIRECTORY),
    }
    if emit:
        emit_json(payload)
    return payload


async def action_remember_skills(emit: bool = True) -> dict[str, Any]:
    state = load_state() or current_config_state()
    if truthy("COGNEE_SELF_IMPROVE_PRUNE"):
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)

    await setup()
    remembered = await cognee.remember(
        str(SKILLS_ROOT),
        dataset_name=state["dataset"],
        content_type="skills",
    )
    dataset_id = getattr(remembered, "dataset_id", None)
    if not dataset_id:
        raise ValueError("Cognee remember(skills) did not return a dataset_id.")

    state.update(
        {
            "status": "skills_remembered",
            "dataset_id": dataset_id,
            "remembered_items": getattr(remembered, "items_processed", None),
        }
    )
    save_state(state)
    payload = {
        "status": "skills_remembered",
        "dataset": state["dataset"],
        "dataset_id": dataset_id,
        "remembered_items": state["remembered_items"],
        "skill_name": state["skill_name"],
        "skill_file": state["skill_file"],
        "state_file": str(STATE_FILE),
    }
    if emit:
        emit_json(payload)
    return payload


async def action_run_agent(emit: bool = True) -> dict[str, Any]:
    state = load_state()
    if not state:
        raise ValueError("State file is missing. Run init-state first.")

    await setup()
    answer = await cognee.search(
        state["task_text"],
        query_type=SearchType.AGENTIC_COMPLETION,
        datasets=state["dataset"],
        skills=[state["skill_name"]],
        max_iter=int(os.environ.get("COGNEE_SKILL_MAX_ITER", "6")),
        session_id=state["session_id"],
    )
    parsed = parse_json_answer(answer)
    agent_score = normalize_score(parsed.get("score"), default=state["eval_score"])
    score = agent_score if state.get("score_from_agent") else state["eval_score"]
    score_source = "agent" if state.get("score_from_agent") else "eval"
    skill_to_improve = str(parsed.get("skill_to_improve") or state["skill_name"])
    result_summary = str(
        parsed.get("result_summary")
        or parsed.get("feedback")
        or "Review missed the dataset ownership boundary check."
    )
    missing_instruction = str(
        parsed.get("missing_instruction")
        or "Always verify tenant/user ownership boundaries before accepting dataset access."
    )
    should_improve = score < state["score_threshold"]

    state.update(
        {
            "status": "agent_completed",
            "agent_answer": unwrap_answer(answer),
            "parsed_agent_answer": parsed,
            "agent_score": agent_score,
            "score": score,
            "score_source": score_source,
            "skill_to_improve": skill_to_improve,
            "skill_file": str(skill_file_for(skill_to_improve)),
            "result_summary": result_summary,
            "missing_instruction": missing_instruction,
            "should_improve": should_improve,
        }
    )
    save_state(state)
    payload = {
        "status": "agent_completed",
        "skill_to_improve": skill_to_improve,
        "score": score,
        "score_source": score_source,
        "agent_score": agent_score,
        "score_threshold": state["score_threshold"],
        "should_improve": should_improve,
        "result_summary": result_summary,
        "missing_instruction": missing_instruction,
        "agent_answer": unwrap_answer(answer),
        "state_file": str(STATE_FILE),
    }
    if emit:
        emit_json(payload)
    return payload


async def action_record_feedback(emit: bool = True) -> dict[str, Any]:
    state = load_state()
    if not state:
        raise ValueError("State file is missing. Run init-state first.")
    if not state.get("should_improve"):
        payload = {
            "status": "no_improvement_needed",
            "reason": "Score is not below the configured threshold.",
            "score": state.get("score"),
            "score_threshold": state.get("score_threshold"),
            "state_file": str(STATE_FILE),
        }
        if emit:
            emit_json(payload)
        return payload

    started_at_ms = int(time.time() * 1000)
    proposal_result = await cognee.remember(
        SkillRunEntry(
            selected_skill_id=state["skill_to_improve"],
            task_text=state["task_text"],
            result_summary=f"{state['result_summary']}\nMissing instruction: {state['missing_instruction']}",
            success_score=state["score"],
            feedback=-1.0,
            started_at_ms=started_at_ms,
            candidate_skill_ids=[state["skill_name"]],
            task_pattern_id="n8n-local-skill-self-improvement",
            router_version="n8n-visible-control-plane-v1",
        ),
        dataset_name=state["dataset"],
        session_id=state["session_id"],
        skill_improvement={
            "skill_name": state["skill_to_improve"],
            "apply": False,
            "score_threshold": state["score_threshold"],
        },
    )
    proposal_id = find_proposal_id(proposal_result)
    proposal_items = [json_safe(item) for item in result_items(proposal_result)]
    status = "proposal_created" if proposal_id else "no_proposal"
    state.update(
        {
            "status": status,
            "proposal_id": proposal_id,
            "proposal_items": proposal_items,
            "approved": bool(state.get("approved")),
        }
    )
    save_state(state)
    payload = {
        "status": status,
        "proposal_id": proposal_id,
        "approved": state["approved"],
        "apply": state["apply"],
        "skill_to_improve": state["skill_to_improve"],
        "score": state["score"],
        "score_threshold": state["score_threshold"],
        "proposal_items": proposal_items,
        "state_file": str(STATE_FILE),
    }
    if emit:
        emit_json(payload)
    return payload


async def action_apply_proposal(emit: bool = True) -> dict[str, Any]:
    state = load_state()
    if not state:
        raise ValueError("State file is missing. Run init-state first.")
    if not state.get("proposal_id"):
        payload = {
            "status": "no_proposal",
            "reason": "No proposal_id exists in state.",
            "state_file": str(STATE_FILE),
        }
        if emit:
            emit_json(payload)
        return payload
    if not truthy_value(state.get("approved")) or not truthy_value(state.get("apply")):
        payload = {
            "status": "waiting_for_review",
            "proposal_id": state["proposal_id"],
            "approved": state.get("approved"),
            "apply": state.get("apply"),
            "state_file": str(STATE_FILE),
        }
        if emit:
            emit_json(payload)
        return payload

    user, dataset = await resolve_dataset_from_state(state)
    skill_to_improve = state["skill_to_improve"]
    before = await skill_body(skill_to_improve, dataset, user)
    applied = await improve_skill(
        skill_to_improve,
        dataset=dataset,
        user=user,
        proposal_id=state["proposal_id"],
        apply=True,
    )
    after = await skill_body(skill_to_improve, dataset, user)
    target_skill_file = skill_file_for(skill_to_improve)
    if truthy_value(state.get("sync_file"), default=True):
        frontmatter, _ = split_frontmatter(target_skill_file.read_text(encoding="utf-8"))
        target_skill_file.write_text(f"{frontmatter}{after.strip()}\n", encoding="utf-8")
    skill_diff = unified_skill_diff(before, after, skill_to_improve)

    state.update(
        {
            "status": "completed",
            "applied": bool(applied),
            "before": one_line(before),
            "after": one_line(after),
            "skill_diff": skill_diff,
        }
    )
    save_state(state)
    payload = {
        "status": "completed",
        "dataset": state["dataset"],
        "session_id": state["session_id"],
        "skill_to_improve": skill_to_improve,
        "score": state["score"],
        "score_source": state["score_source"],
        "agent_score": state["agent_score"],
        "score_threshold": state["score_threshold"],
        "proposal_id": state["proposal_id"],
        "applied": bool(applied),
        "skill_file": str(target_skill_file),
        "before": one_line(before),
        "after": one_line(after),
        "skill_diff": skill_diff,
        "state_file": str(STATE_FILE),
    }
    if emit:
        emit_json(payload)
    return payload


async def action_review_packet(emit: bool = True) -> dict[str, Any]:
    state = load_state()
    if not state:
        raise ValueError("State file is missing. Run init-state first.")
    payload = {
        "status": "review_packet",
        "skill_to_improve": state.get("skill_to_improve", state.get("skill_name")),
        "score": state.get("score"),
        "score_threshold": state.get("score_threshold"),
        "proposal_id": state.get("proposal_id"),
        "approved": state.get("approved"),
        "result_summary": state.get("result_summary"),
        "missing_instruction": state.get("missing_instruction"),
        "state_file": str(STATE_FILE),
    }
    if emit:
        emit_json(payload)
    return payload


async def action_run_full(emit: bool = True) -> dict[str, Any]:
    await action_init_state(emit=False)
    await action_remember_skills(emit=False)
    agent = await action_run_agent(emit=False)
    if not agent["should_improve"]:
        payload = {
            "status": "no_improvement_needed",
            "score": agent["score"],
            "score_threshold": agent["score_threshold"],
            "agent_answer": agent["agent_answer"],
            "state_file": str(STATE_FILE),
        }
        if emit:
            emit_json(payload)
        return payload
    proposal = await action_record_feedback(emit=False)
    if proposal["status"] != "proposal_created":
        payload = {
            "status": proposal["status"],
            "reason": "Cognee did not generate a skill improvement proposal.",
            "score": proposal["score"],
            "score_threshold": proposal["score_threshold"],
            "proposal_items": proposal["proposal_items"],
            "state_file": str(STATE_FILE),
        }
        if emit:
            emit_json(payload)
        return payload
    if not proposal.get("approved") or not proposal.get("apply"):
        payload = await action_review_packet(emit=False)
        if emit:
            emit_json(payload)
        return payload
    payload = await action_apply_proposal(emit=False)
    if emit:
        emit_json(payload)
    return payload


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Cognee skill self-improvement demo.")
    parser.add_argument(
        "action",
        nargs="?",
        default="run-full",
        choices=[
            "run-full",
            "dry-run",
            "smoke",
            "init-state",
            "remember-skills",
            "run-agent",
            "record-feedback",
            "review-packet",
            "apply-proposal",
        ],
    )
    args = parser.parse_args()

    if truthy("COGNEE_SELF_IMPROVE_DRY_RUN") or args.action == "dry-run":
        await action_dry_run()
    elif truthy("COGNEE_SELF_IMPROVE_SMOKE") or args.action == "smoke":
        await action_smoke()
    elif args.action == "init-state":
        await action_init_state()
    elif args.action == "remember-skills":
        await action_remember_skills()
    elif args.action == "run-agent":
        await action_run_agent()
    elif args.action == "record-feedback":
        await action_record_feedback()
    elif args.action == "review-packet":
        await action_review_packet()
    elif args.action == "apply-proposal":
        await action_apply_proposal()
    else:
        await action_run_full()


if __name__ == "__main__":
    asyncio.run(main())
