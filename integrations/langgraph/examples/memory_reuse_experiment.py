"""
Memory Reuse Experiment
=======================

Demonstrates how short-term (Redis) and long-term (knowledge graph) memory
affect incident investigation quality across repeated runs.

Uses the hand-crafted demo incidents (tenant_acme: TICK-1001, TICK-1002)
repeated REPEAT times per condition. All data is loaded and cognified
automatically — no separate load step is required.

Two conditions on the SAME data:
  1) BASELINE             -- graph search only; every incident starts fresh
  2) BUILT-IN FEEDBACK    -- graph search + Redis session cache (short-term)
                             + memify every MEMIFY_EVERY incidents (long-term).
                             The analyst's Q&A history accumulates in Redis so
                             each incident sees prior findings. After every
                             MEMIFY_EVERY incidents the cache is persisted to
                             the knowledge graph and then flushed, so the next
                             batch starts fresh but the graph retains the
                             distilled knowledge permanently.

Memory retrieval flow (per cognee.search call):
  1. Graph search  → factual context from knowledge graph
  2. Redis read    → prior Q&A history prepended to LLM prompt  (feedback only)
  3. LLM call      → answer generated from both sources
  4. Redis write   → Q&A pair stored for the next incident      (feedback only)

Run from the repo root:
    python examples/memory_reuse_experiment.py

Outputs:
  - experiment_results.json  — raw timing + summaries for both conditions
  - experiment_graph.html    — interactive graph visualization (open in browser)
"""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict

import cognee
from cognee.api.v1.visualize.visualize import visualize_multi_user_graph
from cognee.infrastructure.session import get_session_manager
from cognee.memify_pipelines.persist_sessions_in_knowledge_graph import (
    persist_sessions_in_knowledge_graph_pipeline,
)
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.engine.operations.setup import setup
from cognee.modules.users.methods import create_user, get_default_user, get_user_by_email
from cognee.modules.users.permissions.methods import authorized_give_permission_on_datasets
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent as create_agent

from cognee_integration_langgraph import get_sessionized_cognee_tools

load_dotenv()
os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] = "True"

# ── Tunable experiment parameters ────────────────────────────────────────────
# How often (in incidents) to flush Redis sessions to the knowledge graph.
MEMIFY_EVERY = 4
# How many times each base incident is repeated per condition.
REPEAT = 5

# ── Demo user credentials ─────────────────────────────────────────────────────
USER_PASSWORD = "demo"
BILLING_EMAIL = "billing@experiment.example.com"
SUPPORT_EMAIL = "support@experiment.example.com"
ENTITLEMENTS_EMAIL = "entitlements@experiment.example.com"
SUPERVISOR_EMAIL = "supervisor@experiment.example.com"

# Populated at runtime by load_experiment_data().
billing_user = None
support_user = None
entitlements_user = None
supervisor_user = None


# ---------------------------------------------------------------------------
# 1. INCIDENTS  (demo tenant_acme only)
# ---------------------------------------------------------------------------

DEMO_DATA_DIR = Path(__file__).resolve().parent / "data" / "cognee_mock_saas_entitlements_demo"
TENANT_ACME = DEMO_DATA_DIR / "tenant_acme"


def build_incidents() -> list[dict]:
    """Return the two hand-crafted demo incidents with their gold summaries."""
    return [
        {
            "ticket_id": "TICK-1001",
            "workspace_id": "W-332",
            "root_cause_key": "delayed_reconci",
            "gold_summary": (
                "Billing account BA-ACME-001 stayed past_due due to delayed "
                "reconciliation of older invoice INV-94700, causing entitlements "
                "fallback to trial_read_only even though INV-94812 was paid."
            ),
        },
        {
            "ticket_id": "TICK-1002",
            "workspace_id": "W-441",
            "root_cause_key": "delayed_reconci",
            "gold_summary": (
                "Same pattern as TICK-1001: paid invoice but workspace downgraded "
                "because billing account status was not reconciled in time."
            ),
        },
    ]


def _build_all_datasets() -> tuple[list, list, list, list]:
    """Return (all_datasets, billing_names, support_names, entitlements_names).

    Each dataset is a tuple of (name, folder_path, node_sets, owner_role).
    Owner role determines which user ingests it and therefore controls access.
    """
    billing_names, support_names, ent_names = [], [], []
    acme_ds = [
        (
            "acme_subscriptions_core",
            TENANT_ACME / "acme_subscriptions_core",
            ["subscriptions", "source:billing_db"],
            "billing",
        ),
        (
            "acme_billing_finance",
            TENANT_ACME / "acme_billing_finance",
            ["finance", "stripe", "invoices", "payments", "dunning"],
            "billing",
        ),
        (
            "acme_support_tickets",
            TENANT_ACME / "acme_support_tickets",
            ["support", "tickets", "customer:acme"],
            "support",
        ),
        (
            "acme_entitlements_state",
            TENANT_ACME / "acme_entitlements_state",
            ["entitlements", "source:entitlements_service"],
            "entitlements",
        ),
        (
            "acme_product_contracts",
            TENANT_ACME / "acme_product_contracts",
            ["contracts", "legal", "pricing_terms"],
            "entitlements",
        ),
        (
            "acme_audit_event_stream",
            TENANT_ACME / "acme_audit_event_stream",
            ["events", "temporal", "source:event_bus"],
            "entitlements_temporal",
        ),
    ]

    datasets = []
    for name, folder, node_sets, owner in acme_ds:
        datasets.append((name, folder, node_sets, owner))
        if owner == "billing":
            billing_names.append(name)
        elif owner == "support":
            support_names.append(name)
        elif owner in ("entitlements", "entitlements_temporal"):
            ent_names.append(name)

    return datasets, billing_names, support_names, ent_names


(
    ALL_DATASETS,
    BILLING_DATASET_NAMES,
    SUPPORT_DATASET_NAMES,
    ENTITLEMENTS_DATASET_NAMES,
) = _build_all_datasets()


# ---------------------------------------------------------------------------
# 2. LOAD DATA INTO COGNEE
# ---------------------------------------------------------------------------


async def get_or_create_user(email, password):
    try:
        return await create_user(email, password)
    except Exception:
        return await get_user_by_email(email)


async def load_experiment_data():
    """Prune existing data, cognify all tenant_acme datasets, and wire permissions.

    Each dataset is owned by a role-specific user (billing/support/entitlements).
    The supervisor is then granted read access to all datasets so it can
    synthesize findings across roles.
    """
    global billing_user, support_user, entitlements_user, supervisor_user

    print("\n[LOAD] Prune + setup ...")
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    await setup()

    billing_user = await get_or_create_user(BILLING_EMAIL, USER_PASSWORD)
    support_user = await get_or_create_user(SUPPORT_EMAIL, USER_PASSWORD)
    entitlements_user = await get_or_create_user(ENTITLEMENTS_EMAIL, USER_PASSWORD)
    supervisor_user = await get_or_create_user(SUPERVISOR_EMAIL, USER_PASSWORD)
    print(f"[LOAD] Users: billing={billing_user.id}, supervisor={supervisor_user.id}")

    print(f"[LOAD] Adding {len(ALL_DATASETS)} datasets (tenant_acme) ...")
    for ds_name, folder, node_sets, owner in ALL_DATASETS:
        if not folder.exists():
            print(f"  [WARN] Skipping {ds_name}: folder {folder} not found")
            continue
        user = (
            billing_user
            if owner == "billing"
            else (support_user if owner == "support" else entitlements_user)
        )
        await cognee.add(str(folder), dataset_name=ds_name, node_set=node_sets, user=user)

    print("[LOAD] Cognify billing ...")
    b_ids = list((await cognee.cognify(datasets=BILLING_DATASET_NAMES, user=billing_user)).keys())

    print("[LOAD] Cognify support ...")
    s_ids = list((await cognee.cognify(datasets=SUPPORT_DATASET_NAMES, user=support_user)).keys())

    ent_non_temporal = [
        n for n in ENTITLEMENTS_DATASET_NAMES if not n.endswith("_audit_event_stream")
    ]
    temporal_names = [n for n in ENTITLEMENTS_DATASET_NAMES if n.endswith("_audit_event_stream")]

    print("[LOAD] Cognify entitlements (non-temporal) ...")
    e_ids = list((await cognee.cognify(datasets=ent_non_temporal, user=entitlements_user)).keys())

    print("[LOAD] Cognify events ...")
    e_ids += list((await cognee.cognify(datasets=temporal_names, user=entitlements_user)).keys())

    # Grant supervisor read access to all datasets for cross-role synthesis.
    print("[LOAD] Sharing permissions to supervisor ...")
    await authorized_give_permission_on_datasets(supervisor_user.id, b_ids, "read", billing_user.id)
    await authorized_give_permission_on_datasets(supervisor_user.id, s_ids, "read", support_user.id)
    await authorized_give_permission_on_datasets(
        supervisor_user.id, e_ids, "read", entitlements_user.id
    )
    # Support and entitlements also need billing data for cross-role queries.
    await authorized_give_permission_on_datasets(support_user.id, b_ids, "read", billing_user.id)
    await authorized_give_permission_on_datasets(
        entitlements_user.id, b_ids, "read", billing_user.id
    )

    print(f"[LOAD] Done. {len(b_ids) + len(s_ids) + len(e_ids)} dataset(s) cognified.\n")


# ---------------------------------------------------------------------------
# 3. AGENT + SUPERVISOR GRAPH
# ---------------------------------------------------------------------------

ConditionMode = Literal["baseline", "builtin_feedback"]


class IncidentState(TypedDict):
    incident_id: str
    workspace_id: str
    run_index: int
    condition: ConditionMode
    findings_billing: str
    findings_support: str
    findings_entitlements: str
    summary: str


def _make_agent(role: str, session_id: str, user):
    """Create a ReAct agent with Cognee tools bound to a specific session + user."""
    tools = get_sessionized_cognee_tools(session_id=session_id, user=user)
    return create_agent("openai:gpt-4o-mini", tools=tools)


async def _query_agent(agent, role: str, question: str) -> str:
    prompt = f"You are a {role}. Search your knowledge base, then answer concisely.\n\n{question}"
    response = await agent.ainvoke({"messages": [HumanMessage(content=prompt)]})
    return response["messages"][-1].content


# Each specialist uses a fully unique session ID (condition + ticket + run + role)
# so their searches never accumulate cross-run history regardless of condition.
async def call_billing(state: IncidentState) -> dict:
    sid = f"exp_{state['condition']}_{state['incident_id']}_r{state['run_index']}_billing"
    agent = _make_agent("billing specialist", sid, billing_user)
    r = await _query_agent(
        agent,
        "billing specialist",
        f"What is the invoice and billing account status for ticket {state['incident_id']}, "
        f"workspace {state['workspace_id']}?",
    )
    return {"findings_billing": r}


async def call_support(state: IncidentState) -> dict:
    sid = f"exp_{state['condition']}_{state['incident_id']}_r{state['run_index']}_support"
    agent = _make_agent("support specialist", sid, support_user)
    r = await _query_agent(
        agent,
        "support specialist",
        f"What did the customer report for ticket {state['incident_id']}?",
    )
    return {"findings_support": r}


async def call_entitlements(state: IncidentState) -> dict:
    sid = f"exp_{state['condition']}_{state['incident_id']}_r{state['run_index']}_ent"
    agent = _make_agent("entitlements specialist", sid, entitlements_user)
    r = await _query_agent(
        agent,
        "entitlements specialist",
        f"Why was workspace {state['workspace_id']} downgraded?",
    )
    return {"findings_entitlements": r}


SYNTHESIZE_PROMPT = (
    "You are an incident analyst. Search your knowledge base for similar past "
    "incidents (customer paid but was downgraded), then use those insights "
    "together with the findings below to write a 2-sentence root cause and "
    "1-sentence fix.\n\n"
    "{context}"
)


async def synthesize(state: IncidentState) -> dict:
    """Analyst synthesizes a root cause from the three specialist findings.

    Session ID strategy:
    - builtin_feedback: one shared session ("exp_builtin_analyst") across ALL
      incidents so Q&A history accumulates in Redis, giving each run the
      benefit of prior investigations.
    - baseline: unique session per (incident, run) so no history accumulates
      and each run is truly independent.
    """
    context = (
        f"Billing: {state['findings_billing']}\n"
        f"Support: {state['findings_support']}\n"
        f"Entitlements: {state['findings_entitlements']}"
    )

    if state["condition"] == "builtin_feedback":
        session_id = "exp_builtin_analyst"
    else:
        session_id = f"exp_baseline_{state['incident_id']}_r{state['run_index']}"

    agent = _make_agent("incident analyst", session_id, supervisor_user)
    response = await agent.ainvoke(
        {"messages": [HumanMessage(content=SYNTHESIZE_PROMPT.format(context=context))]}
    )
    return {"summary": response["messages"][-1].content}


# Wire the four nodes into a linear pipeline: billing → support → entitlements → synthesize.
graph_builder = StateGraph(IncidentState)
graph_builder.add_node("billing", call_billing)
graph_builder.add_node("support", call_support)
graph_builder.add_node("entitlements", call_entitlements)
graph_builder.add_node("synthesize", synthesize)
graph_builder.add_edge(START, "billing")
graph_builder.add_edge("billing", "support")
graph_builder.add_edge("support", "entitlements")
graph_builder.add_edge("entitlements", "synthesize")
graph_builder.add_edge("synthesize", END)
supervisor = graph_builder.compile()


# ---------------------------------------------------------------------------
# 4. EXPERIMENT RUNNER
# ---------------------------------------------------------------------------

# Collects session IDs from each feedback incident so we can batch-persist
# them to the knowledge graph every MEMIFY_EVERY incidents.
_feedback_session_ids: list[str] = []

# (label, depth, is_memify_point) — built up during the builtin_feedback
# condition and printed as an ASCII bar chart at the end.
_depth_log: list[tuple[str, int, bool]] = []


async def _get_session_entries(user_id: str, session_id: str) -> list:
    """Return raw Q&A entries from Redis for a given user + session."""
    sm = get_session_manager()
    if not sm.is_available:
        return []
    return await sm.get_session(user_id=user_id, session_id=session_id) or []


def _print_session_snapshot(label: str, entries: list) -> None:
    """Print every Q&A entry currently stored in the Redis session cache."""
    w = 72
    print(f"\n{'─' * w}")
    print(f"  SESSION SNAPSHOT [{label}]  —  {len(entries)} Q&A pair(s) in cache")
    print(f"{'─' * w}")
    if not entries:
        print("  (empty)\n")
        return
    for i, e in enumerate(entries, 1):
        q = e.get("question", "")
        a = e.get("answer", "")
        t = e.get("time", "")[:19]
        print(f"  [{i}] {t}")
        print(f"      Q: {q[:110]}{'…' if len(q) > 110 else ''}")
        print(f"      A: {a[:160]}{'…' if len(a) > 160 else ''}")
    print(f"{'─' * w}\n")


def _print_depth_chart() -> None:
    """ASCII bar chart showing how the Redis session depth grew and drained over time.

    Each bar is the number of Q&A pairs in the shared analyst session after
    that incident completed. A MEMIFY marker shows when the cache was flushed
    to the knowledge graph.
    """
    if not _depth_log:
        return
    max_depth = max(d for _, d, _ in _depth_log) or 1
    bar_width = 38
    w = 72
    print(f"\n{'=' * w}")
    print("  SESSION CACHE DEPTH  (exp_builtin_analyst — Q&A pairs stored per run)")
    print(f"{'=' * w}")
    for label, depth, is_memify in _depth_log:
        bar = "█" * int((depth / max_depth) * bar_width)
        print(f"  {label:<22} │{bar} {depth}")
        if is_memify:
            print(f"  {'':22} └── [MEMIFY → persisted to graph]")
    print(f"{'=' * w}\n")


async def _flush_session(user_id: str, session_id: str) -> bool:
    """Delete all Q&A entries for a session from Redis after a successful memify.

    This ensures the next batch of incidents starts with an empty short-term
    cache while the knowledge graph retains the distilled long-term memory.
    """
    sm = get_session_manager()
    if not sm.is_available:
        return False
    return await sm.delete_session(user_id=user_id, session_id=session_id)


async def _persist_accumulated_sessions() -> float:
    """Persist cached Q&A sessions to the knowledge graph, then flush Redis.

    Returns the wall-clock time spent in the memify pipeline (seconds).
    The supervisor user's session is used because the analyst agent runs
    under supervisor_user and that is the user_id stored in the cache entries.
    """
    if not _feedback_session_ids:
        return 0.0

    default_user = await get_default_user()
    ids_to_persist = list(set(_feedback_session_ids))
    _feedback_session_ids.clear()

    # The analyst session is keyed by supervisor_user.id in Redis because
    # cognee.search() sets session_user to the user passed to the tool.
    user_id = str(supervisor_user.id)
    entries_before = await _get_session_entries(user_id, "exp_builtin_analyst")
    _print_session_snapshot("BEFORE MEMIFY", entries_before)

    start = time.perf_counter()
    memify_ok = False
    try:
        await persist_sessions_in_knowledge_graph_pipeline(
            user=default_user,
            session_ids=ids_to_persist,
            dataset="exp_feedback_sessions",
        )
        memify_ok = True
    except Exception as e:
        print(f"    [WARN] Memify persist failed: {e}")
    elapsed = time.perf_counter() - start

    # Only flush after a confirmed successful persist so we don't lose data.
    if memify_ok:
        for sid in ids_to_persist:
            await _flush_session(user_id, sid)

    entries_after = await _get_session_entries(user_id, "exp_builtin_analyst")
    _print_session_snapshot("AFTER MEMIFY", entries_after)

    return elapsed


async def run_one_incident(incident: dict, condition: ConditionMode) -> dict:
    initial: IncidentState = {
        "incident_id": incident["ticket_id"],
        "workspace_id": incident["workspace_id"],
        "run_index": incident.get("run_index", 1),
        "findings_billing": "",
        "findings_support": "",
        "findings_entitlements": "",
        "summary": "",
        "condition": condition,
    }

    start = time.perf_counter()
    result = await supervisor.ainvoke(initial)
    elapsed = time.perf_counter() - start

    if condition == "builtin_feedback":
        _feedback_session_ids.append("exp_builtin_analyst")

    return {
        "incident_id": incident["ticket_id"],
        "run_index": incident.get("run_index", 1),
        "root_cause_key": incident["root_cause_key"],
        "elapsed_s": round(elapsed, 1),
        "store_s": 0.0,
        "summary": result["summary"],
        "gold_summary": incident["gold_summary"],
    }


async def run_condition(label: str, incidents: list[dict], condition: ConditionMode) -> list[dict]:
    print(f"\n{'=' * 70}")
    print(f"  CONDITION: {label}")
    print(f"{'=' * 70}")

    _feedback_session_ids.clear()
    _depth_log.clear()
    results = []
    total_memify_time = 0.0

    for i, inc in enumerate(incidents):
        run_idx = inc.get("run_index", 1)
        tag = f"[{label}] {inc['ticket_id']} #{run_idx} ({i + 1}/{len(incidents)})"
        print(f"  {tag} running ...", end="", flush=True)
        r = await run_one_incident(inc, condition=condition)
        print(f" {r['elapsed_s']}s")
        results.append(r)

        if condition == "builtin_feedback":
            # Record depth for the chart. Mark this row if it is a memify point
            # so the chart can annotate the flush boundary.
            is_last = i + 1 == len(incidents)
            is_memify_point = (i + 1) % MEMIFY_EVERY == 0 or (is_last and _feedback_session_ids)
            entries = await _get_session_entries(str(supervisor_user.id), "exp_builtin_analyst")
            _depth_log.append((f"{inc['ticket_id']} #{run_idx}", len(entries), is_memify_point))

        if condition == "builtin_feedback" and (i + 1) % MEMIFY_EVERY == 0:
            print(
                f"    [MEMIFY] Persisting {len(_feedback_session_ids)} sessions ...",
                end="",
            )
            memify_time = await _persist_accumulated_sessions()
            total_memify_time += memify_time
            print(f" {memify_time:.1f}s")

    # Flush any sessions that didn't hit the MEMIFY_EVERY boundary.
    if condition == "builtin_feedback" and _feedback_session_ids:
        print(
            f"    [MEMIFY] Persisting remaining {len(_feedback_session_ids)} sessions ...",
            end="",
        )
        memify_time = await _persist_accumulated_sessions()
        total_memify_time += memify_time
        print(f" {memify_time:.1f}s")

    if condition == "builtin_feedback":
        # Spread total memify overhead evenly across all incidents for fair comparison.
        for r in results:
            r["store_s"] = round(total_memify_time / len(results), 1)

    total_time = sum(r["elapsed_s"] for r in results)
    print(
        f"\n  Investigation total: {total_time:.0f}s  |  "
        f"Avg: {total_time / len(results):.1f}s per incident\n"
    )
    return results


# ---------------------------------------------------------------------------
# 5. REPORT + VISUALIZATION
# ---------------------------------------------------------------------------


def _total_time(results: list[dict]) -> float:
    return sum(r["elapsed_s"] for r in results)


def print_report(baseline: list[dict], builtin_fb: list[dict]) -> None:
    w = 60
    print("\n" + "=" * w)
    print("  EXPERIMENT RESULTS  (time comparison)")
    print("=" * w)

    header = f"{'Incident':<15} | {'Cause':<16} | {'Base':>6} | {'Feedback':>6}"
    print(header)
    print("-" * len(header))

    for bl, bf in zip(baseline, builtin_fb):
        cause = bl.get("root_cause_key", "?")[:16]
        display_id = f"{bl['incident_id']} #{bl.get('run_index', 1)}"
        print(
            f"{display_id:<15} | {cause:<16} | {bl['elapsed_s']:>4.0f}s | {bf['elapsed_s']:>4.0f}s"
        )

    tot_bl = _total_time(baseline)
    tot_bf = _total_time(builtin_fb)
    print("-" * len(header))
    print(f"{'TOTAL':<15} | {'':16} | {tot_bl:>4.0f}s | {tot_bf:>4.0f}s")
    print("=" * w + "\n")

    output = {
        "timestamp": datetime.now().isoformat(),
        "n_incidents": len(baseline),
        "memify_every": MEMIFY_EVERY,
        "conditions": {
            "baseline": {
                "description": "Cognee graph search, no feedback stored",
                "investigation_time_s": tot_bl,
                "results": baseline,
            },
            "builtin_feedback": {
                "description": (
                    f"Cognee + Redis session cache + memify every {MEMIFY_EVERY} incidents"
                ),
                "investigation_time_s": tot_bf,
                "results": builtin_fb,
            },
        },
    }
    out_path = "experiment_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Raw results saved to {out_path}\n")


async def _visualize_experiment_graph() -> None:
    """Save a combined interactive HTML visualization of all user graphs.

    Combines all four user graphs (billing, support, entitlements, and
    default_user who owns the memified sessions) into a single view.
    Nodes are tagged with source_user so you can visually distinguish
    the original SaaS data from the memified session nodes.
    """
    print("[VIZ] Building combined graph visualization ...")
    default_user = await get_default_user()

    user_dataset_pairs = []
    for user, dataset_names in [
        (billing_user, BILLING_DATASET_NAMES),
        (support_user, SUPPORT_DATASET_NAMES),
        (entitlements_user, ENTITLEMENTS_DATASET_NAMES),
        (default_user, ["exp_feedback_sessions"]),  # memified session nodes
    ]:
        datasets = await get_authorized_existing_datasets(
            user=user, datasets=dataset_names, permission_type="read"
        )
        for ds in datasets:
            user_dataset_pairs.append((user, ds))

    out_path = Path(__file__).resolve().parent / "experiment_graph.html"
    try:
        await visualize_multi_user_graph(user_dataset_pairs, destination_file_path=str(out_path))
        print(f"[VIZ] Graph saved to {out_path}")
        print(f"[VIZ] Open in browser: file://{out_path}")
    except Exception as e:
        print(f"[VIZ] Visualization failed: {e}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def _enable_caching() -> None:
    """Ensure the CACHING env var is set so the SessionManager is active.

    The real requirement is CACHING=true in .env (read by pydantic-settings
    at import time before this function runs). This call is a belt-and-suspenders
    guard in case the .env value is missing.
    """
    os.environ["CACHING"] = "true"


async def _create_feedback_dataset_for_default_user() -> None:
    """Bootstrap the dataset that will hold memified session nodes.

    The memify pipeline (persist_sessions_in_knowledge_graph_pipeline) runs
    cognify internally as get_default_user(), so the target dataset must be
    owned by that same user before the first memify call.
    """
    default_user = await get_default_user()
    try:
        await cognee.add(
            "Feedback session bootstrap",
            dataset_name="exp_feedback_sessions",
            node_set=["user_sessions_from_cache"],
            user=default_user,
        )
        await cognee.cognify(datasets=["exp_feedback_sessions"], user=default_user)
    except Exception as e:
        print(f"  [WARN] Feedback dataset bootstrap: {e}")


async def main() -> None:
    base_incidents = build_incidents()
    # Expand each incident REPEAT times, tagging each copy with its run index.
    incidents = [{**inc, "run_index": r + 1} for r in range(REPEAT) for inc in base_incidents]

    print(f"\n{'=' * 70}")
    print(f"  MEMORY REUSE EXPERIMENT  --  {len(incidents)} incidents, 2 conditions")
    print(f"{'=' * 70}")
    print(
        "\nThis experiment runs the same multi-agent supervisor pipeline on\n"
        "SaaS incidents from the demo (tenant_acme) under two conditions:\n"
        "  1) BASELINE           -- Cognee graph search, no feedback\n"
        f"  2) + BUILT-IN FEEDBACK -- Redis cache + memify every {MEMIFY_EVERY} incidents\n"
    )
    print(
        f"Using {len(base_incidents)} unique incidents x {REPEAT} repeats = "
        f"{len(incidents)} total runs.\n"
    )

    await load_experiment_data()

    # Caching must be enabled before either condition runs. Baseline isolation
    # is achieved structurally: each baseline incident uses a unique session ID
    # (condition + ticket + run_index) so no history accumulates there.
    _enable_caching()

    # ── Condition 1: baseline (graph search only, no session history) ──────
    results_baseline = await run_condition("BASELINE", incidents, condition="baseline")

    # ── Condition 2: feedback (Redis cache + periodic memify to graph) ──────
    print(f"\n[SETUP] Running built-in feedback condition (memify every {MEMIFY_EVERY}) ...")
    await _create_feedback_dataset_for_default_user()
    results_builtin = await run_condition(
        "BUILT-IN FEEDBACK", incidents, condition="builtin_feedback"
    )

    print_report(results_baseline, results_builtin)
    _print_depth_chart()
    await _visualize_experiment_graph()


if __name__ == "__main__":
    if not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY not set. Add it to your .env file.")
        exit(1)
    if not os.getenv("LLM_API_KEY"):
        os.environ["LLM_API_KEY"] = os.environ["OPENAI_API_KEY"]

    asyncio.run(main())
