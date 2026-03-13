import asyncio
import os
from pathlib import Path

import cognee
from cognee import SearchType
from cognee.modules.engine.operations.setup import setup
from cognee.modules.users.methods import create_user
from cognee.modules.users.permissions.methods import (
    authorized_give_permission_on_datasets,
)

BASE = Path(__file__).resolve().parent

TENANT_ACME = BASE / "tenant_acme"
ONTOLOGY_PATH = BASE / "ontologies" / "saas_finance_subset.ttl"

# User credentials (shared with saas_entitlements_agents.py)
BILLING_USER_EMAIL = "billing@demo-saas.example.com"
SUPPORT_USER_EMAIL = "support@demo-saas.example.com"
ENTITLEMENTS_USER_EMAIL = "entitlements@demo-saas.example.com"
SUPERVISOR_USER_EMAIL = "supervisor@demo-saas.example.com"
USER_PASSWORD = "demo"

# All datasets with their node_sets
DATASETS = [
    (
        "acme_subscriptions_core",
        TENANT_ACME / "acme_subscriptions_core",
        ["subscriptions", "source:billing_db"],
    ),
    (
        "acme_entitlements_state",
        TENANT_ACME / "acme_entitlements_state",
        ["entitlements", "source:entitlements_service"],
    ),
    (
        "acme_billing_finance",
        TENANT_ACME / "acme_billing_finance",
        ["finance", "stripe", "invoices", "payments", "dunning"],
    ),
    (
        "acme_support_tickets",
        TENANT_ACME / "acme_support_tickets",
        ["support", "tickets", "customer:acme"],
    ),
    (
        "acme_product_contracts",
        TENANT_ACME / "acme_product_contracts",
        ["contracts", "legal", "pricing_terms"],
    ),
    (
        "acme_audit_event_stream",
        TENANT_ACME / "acme_audit_event_stream",
        ["events", "temporal", "source:event_bus"],
    ),
    # Agent-private datasets
    (
        "billing_agent_private_notes",
        TENANT_ACME / "agent_private_datasets" / "billing_agent_private_notes",
        ["private", "billing_agent"],
    ),
    (
        "entitlements_agent_private_notes",
        TENANT_ACME / "agent_private_datasets" / "entitlements_agent_private_notes",
        ["private", "entitlements_agent"],
    ),
    (
        "support_agent_private_notes",
        TENANT_ACME / "agent_private_datasets" / "support_agent_private_notes",
        ["private", "support_agent"],
    ),
]

# Dataset groups by owner (dataset names).
BILLING_DATASET_NAMES = [
    "acme_subscriptions_core",
    "acme_billing_finance",
    "billing_agent_private_notes",
]
SUPPORT_DATASET_NAMES = [
    "acme_support_tickets",
    "support_agent_private_notes",
]
ENTITLEMENTS_DATASET_NAMES = [
    "acme_entitlements_state",
    "acme_product_contracts",
    "acme_audit_event_stream",
    "entitlements_agent_private_notes",
]


def _datasets_for(names):
    """Filter DATASETS list by name."""
    return [d for d in DATASETS if d[0] in names]


def _extract_dataset_ids(cognify_result):
    """Extract dataset_ids from cognify output dictionary."""
    return list(cognify_result.keys())


async def main():
    os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] = "True"

    print("\n>>> Prune + setup ...")
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    await setup()
    os.environ["ONTOLOGY_FILE_PATH"] = str(ONTOLOGY_PATH)
    print(">>> Done prune + setup\n")

    # Create users
    print(">>> Creating users ...")
    billing_user = await create_user(BILLING_USER_EMAIL, USER_PASSWORD)
    support_user = await create_user(SUPPORT_USER_EMAIL, USER_PASSWORD)
    entitlements_user = await create_user(ENTITLEMENTS_USER_EMAIL, USER_PASSWORD)
    supervisor_user = await create_user(SUPERVISOR_USER_EMAIL, USER_PASSWORD)
    print(f"  billing_user:      {billing_user.id}")
    print(f"  support_user:      {support_user.id}")
    print(f"  entitlements_user: {entitlements_user.id}")
    print(f"  supervisor_user:   {supervisor_user.id}")
    print(">>> Done creating users\n")

    # --- Add datasets per user ---
    print(">>> Adding datasets per user ...")
    for ds_name, folder, node_sets in _datasets_for(BILLING_DATASET_NAMES):
        await cognee.add(str(folder), dataset_name=ds_name, node_set=node_sets, user=billing_user)
    for ds_name, folder, node_sets in _datasets_for(SUPPORT_DATASET_NAMES):
        await cognee.add(str(folder), dataset_name=ds_name, node_set=node_sets, user=support_user)
    for ds_name, folder, node_sets in _datasets_for(ENTITLEMENTS_DATASET_NAMES):
        await cognee.add(
            str(folder),
            dataset_name=ds_name,
            node_set=node_sets,
            user=entitlements_user,
        )
    print(">>> Done adding datasets\n")

    # --- Cognify per user ---
    print(">>> Cognify (billing_user) ...")
    billing_cognify = await cognee.cognify(datasets=BILLING_DATASET_NAMES, user=billing_user)
    billing_ds_ids = _extract_dataset_ids(billing_cognify)
    print(">>> Done cognify billing\n")

    print(">>> Cognify (support_user) ...")
    support_cognify = await cognee.cognify(datasets=SUPPORT_DATASET_NAMES, user=support_user)
    support_ds_ids = _extract_dataset_ids(support_cognify)
    print(">>> Done cognify support\n")

    # Entitlements: non-temporal datasets first, then temporal for audit stream
    ent_non_temporal = [n for n in ENTITLEMENTS_DATASET_NAMES if n != "acme_audit_event_stream"]
    print(">>> Cognify (entitlements_user, non-temporal) ...")
    ent_cognify = await cognee.cognify(datasets=ent_non_temporal, user=entitlements_user)
    ent_ds_ids = _extract_dataset_ids(ent_cognify)
    print(">>> Done cognify entitlements (non-temporal)\n")

    print(">>> Cognify (entitlements_user, temporal) ...")
    ent_temporal_cognify = await cognee.cognify(
        datasets=["acme_audit_event_stream"],
        user=entitlements_user,
        temporal_cognify=True,
    )

    ent_ds_ids += _extract_dataset_ids(ent_temporal_cognify)
    print(">>> Done cognify entitlements (temporal)\n")

    # --- Share permissions ---
    print(">>> Sharing permissions ...")

    # Supervisor gets read on everything
    await authorized_give_permission_on_datasets(
        supervisor_user.id, billing_ds_ids, "read", billing_user.id
    )
    await authorized_give_permission_on_datasets(
        supervisor_user.id, support_ds_ids, "read", support_user.id
    )
    await authorized_give_permission_on_datasets(
        supervisor_user.id, ent_ds_ids, "read", entitlements_user.id
    )

    # Support and Entitlements agents need read on billing datasets (for subscriptions_core)
    await authorized_give_permission_on_datasets(
        support_user.id, billing_ds_ids, "read", billing_user.id
    )
    await authorized_give_permission_on_datasets(
        entitlements_user.id, billing_ds_ids, "read", billing_user.id
    )

    print("  supervisor_user has read on all datasets")
    print("  support_user has read on billing datasets (for subscriptions_core)")
    print("  entitlements_user has read on billing datasets (for subscriptions_core)")
    print(">>> Done sharing permissions\n")

    # --- Sanity search ---
    print(">>> Search (sanity check as supervisor_user) ...")
    res = await cognee.search(
        query_type=SearchType.GRAPH_COMPLETION,
        query_text="Explain why AcmeCorp was downgraded to trial_read_only even though invoice "
        "INV-94812 is paid. Include timestamps and the likely fix.",
        dataset_ids=billing_ds_ids + support_ds_ids + ent_ds_ids,
        user=supervisor_user,
        session_id="case_ACME_TICK-1001",
    )
    print("\n--- Graph completion result ---\n", res)

    res2 = await cognee.search(
        query_type=SearchType.TEMPORAL,
        query_text="What happened between 2026-01-03 08:15 and 2026-01-03 09:10 "
        "for workspace W-332?",
        dataset_ids=ent_ds_ids,
        user=entitlements_user,
        top_k=10,
        session_id="case_ACME_TICK-1001",
    )
    print("\n--- Temporal result ---\n", res2)
    print(">>> Done search\n")

    print(">>> Storing feedback as resolution in agent_resolutions dataset ...")
    await cognee.add(
        "Good answer. Next time: put the invoice id and paid_at timestamp in the first "
        "sentence, and mention the grace period rule.",
        dataset_name="agent_resolutions",
        node_set=["resolutions"],
        user=supervisor_user,
    )
    await cognee.cognify(datasets=["agent_resolutions"], user=supervisor_user)
    print(">>> Done.\n")


if __name__ == "__main__":
    asyncio.run(main())
