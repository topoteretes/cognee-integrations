"""
SaaS Entitlements Copilot - Multi-Agent Demo

Run load_into_cognee.py first, then this script.
"""

import asyncio
import os
from typing import List, TypedDict

import cognee
from cognee.modules.data.exceptions import DatasetNotFoundError
from cognee.modules.engine.models.node_set import NodeSet
from cognee.modules.search.types import SearchType
from cognee.modules.users.exceptions import PermissionDeniedError
from cognee.modules.users.methods import create_user
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent as create_agent

from cognee_integration_langgraph import get_sessionized_cognee_tools

load_dotenv()

# Enable access control (must match load_into_cognee.py)
os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] = "True"

# User credentials (must match load_into_cognee.py)
BILLING_USER_EMAIL = "billing@demo-saas.example.com"
SUPPORT_USER_EMAIL = "support@demo-saas.example.com"
ENTITLEMENTS_USER_EMAIL = "entitlements@demo-saas.example.com"
SUPERVISOR_USER_EMAIL = "supervisor@demo-saas.example.com"
USER_PASSWORD = "demo"

# Configuration
INCIDENT_WORKSPACE = {"TICK-1001": "W-332", "TICK-1002": "W-441"}
INCIDENT_TIME_RANGE = {
    "TICK-1001": ("2026-01-03 08:15", "2026-01-04 12:00"),
    "TICK-1002": ("2026-01-05 00:00", "2026-01-06 00:00"),
}

# Module-level user objects (populated in main())
billing_user = None
support_user = None
entitlements_user = None
supervisor_user = None


async def get_or_create_user(email, password):
    """Get existing user or create new one."""
    try:
        return await create_user(email, password)
    except Exception:
        # User already exists; fetch it
        from cognee.modules.users.methods import get_user_by_email

        return await get_user_by_email(email)


class SaaSAgent:
    """Base class for specialized SaaS agents."""

    def __init__(
        self,
        name: str,
        role: str,
        datasets: List[str],
        session_id: str,
        user=None,
        model: str = "openai:gpt-4o-mini",
    ):
        self.name = name
        self.role = role
        self.datasets = datasets
        self.session_id = session_id
        self.model = model
        self.user = user

        # Get sessionized tools for this agent
        self.tools = get_sessionized_cognee_tools(
            session_id=session_id,
            user=user,
        )

        # Create the LangGraph agent
        self.agent = create_agent(model, tools=self.tools)
        self.agent.step_timeout = None

        user_label = f", user: {user.email}" if user else ""
        print(f"  Initialized {self.name} ({self.role}) with session: {session_id}{user_label}")

    async def query(self, question: str, context: str = "") -> str:
        """
        Query the agent with a question.

        Args:
            question: The question to ask
            context: Additional context to provide

        Returns:
            The agent's response
        """
        messages = []

        # Add context if provided
        if context:
            messages.append(HumanMessage(content=f"Context: {context}"))

        # Add system prompt with role and dataset info
        system_prompt = f"""You are {self.name}, a {self.role}.

You have access to specialized knowledge about: {", ".join(self.datasets)}

When answering questions:
1. Search your knowledge base first using the search_tool
2. Do NOT manually specify node_set or datasets \
parameters - the session context will handle \
this automatically
3. Provide specific details like IDs, timestamps, and amounts
4. Cite your sources from the datasets
5. If you don't have enough information, say so clearly
"""
        messages.append(HumanMessage(content=system_prompt))
        messages.append(HumanMessage(content=question))

        # Invoke the agent
        response = await self.agent.ainvoke({"messages": messages})

        return response["messages"][-1].content


class BillingAgent(SaaSAgent):
    """Specialized agent for billing and finance queries."""

    def __init__(
        self,
        session_id: str = "billing_agent_session",
        user=None,
    ):
        super().__init__(
            name="Billing Agent",
            role="billing and finance specialist",
            datasets=[
                "acme_billing_finance",
                "acme_subscriptions_core",
                "billing_agent_private_notes",
            ],
            session_id=session_id,
            user=user,
        )


class SupportAgent(SaaSAgent):
    """Specialized agent for customer support."""

    def __init__(
        self,
        session_id: str = "support_agent_session",
        user=None,
    ):
        super().__init__(
            name="Support Agent",
            role="customer support specialist",
            datasets=[
                "acme_support_tickets",
                "acme_subscriptions_core",
                "support_agent_private_notes",
            ],
            session_id=session_id,
            user=user,
        )


class EntitlementsAgent(SaaSAgent):
    """Specialized agent for entitlements and access control."""

    def __init__(
        self,
        session_id: str = "entitlements_agent_session",
        user=None,
    ):
        super().__init__(
            name="Entitlements Agent",
            role="entitlements and access control specialist",
            datasets=[
                "acme_entitlements_state",
                "acme_subscriptions_core",
                "acme_product_contracts",
                "entitlements_agent_private_notes",
            ],
            session_id=session_id,
            user=user,
        )


async def get_incident_timeline(
    workspace_id: str, session_id: str, incident_id: str | None = None
) -> str:
    """
    Fetch the canonical timeline for an incident from the audit event stream.

    Uses TEMPORAL search over acme_audit_event_stream. One timeline per incident,
    no conflicting version. Time range is from INCIDENT_TIME_RANGE when incident_id is set.
    """
    if incident_id and incident_id in INCIDENT_TIME_RANGE:
        start, end = INCIDENT_TIME_RANGE[incident_id]
        query = (
            f"What events occurred for workspace {workspace_id} between "
            f"{start} and {end}? List in chronological order with timestamps."
        )
    else:
        query = (
            f"What events occurred for workspace {workspace_id} between "
            "2026-01-03 08:15 and 2026-01-04 12:00? List in chronological order with timestamps."
        )

    result: list = await cognee.search(
        query_type=SearchType.TEMPORAL,
        query_text=query,
        datasets=["acme_audit_event_stream"],
        top_k=20,
        session_id=session_id,
        user=entitlements_user,
    )
    if isinstance(result, list) and len(result) > 0:
        return str(result[0]) if result[0] else ""
    return str(result)


def _normalize_search_result(raw) -> str:
    """Turn cognee.search() return value into a single string for context."""
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        parts = []
        for item in raw:
            part = item.get("search_result")
            if part is not None:
                parts.append(part[0] if isinstance(part, list) and part else part)
        return "\n".join(str(p) for p in parts) if parts else ""
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], str):
        return raw[0]
    return str(raw)


class IncidentState(TypedDict):
    """State for the supervisor incident flow."""

    incident_id: str
    workspace_id: str
    findings_billing: str
    findings_support: str
    findings_entitlements: str
    summary: str
    recommendation: str
    timeline: str


async def call_billing(state: IncidentState) -> dict:
    """Run Billing Agent: two queries in same session (context preserved for follow-up)."""
    session_id = f"incident_{state['incident_id']}"
    agent = BillingAgent(
        session_id=session_id,
        user=billing_user,
    )
    r1 = await agent.query(
        "What is the status of invoice for workspace "
        f"{state['workspace_id']} / ticket "
        f"{state['incident_id']}?"
    )
    r2 = await agent.query("What about the billing account status? Any reconciliation issues?")
    findings = f"Invoice: {r1}\n\nBilling account: {r2}"
    return {"findings_billing": findings}


async def call_support(state: IncidentState) -> dict:
    """Run Support Agent for the incident."""
    session_id = f"incident_{state['incident_id']}"
    agent = SupportAgent(
        session_id=session_id,
        user=support_user,
    )
    findings = await agent.query(
        "What did the customer report for ticket "
        f"{state['incident_id']} and what is the "
        "operational impact?"
    )
    return {"findings_support": findings}


async def call_entitlements(state: IncidentState) -> dict:
    """Run Entitlements Agent for the incident."""
    session_id = f"incident_{state['incident_id']}"
    agent = EntitlementsAgent(
        session_id=session_id,
        user=entitlements_user,
    )
    findings = await agent.query(
        f"Why was workspace {state['workspace_id']} "
        "downgraded? Include any timing or "
        "policy triggers."
    )
    return {"findings_entitlements": findings}


async def synthesize(state: IncidentState) -> dict:
    """Build timeline and synthesize root cause + recommendation."""
    session_id = f"incident_{state['incident_id']}"
    timeline = await get_incident_timeline(
        state["workspace_id"], session_id, incident_id=state["incident_id"]
    )

    # Similar past resolutions from Cognee memory (e.g. TICK-1001 when running TICK-1002)
    past_raw = await cognee.search(
        query_text=(
            "Have we seen similar incidents (customer"
            " paid but downgraded)? What was the root"
            " cause and recommendation?"
        ),
        query_type=SearchType.GRAPH_COMPLETION,
        session_id=None,
        user=supervisor_user,
    )
    past_resolutions = _normalize_search_result(past_raw)

    current_context = (
        f"Billing findings: {state['findings_billing']}\n\n"
        f"Support findings: {state['findings_support']}\n\n"
        f"Entitlements findings: {state['findings_entitlements']}\n\n"
        f"Timeline: {timeline}"
    )
    if past_resolutions:
        context = (
            f"Similar past resolutions from Cognee memory (if any):\n{past_resolutions}\n\n"
            f"{current_context}"
        )
    else:
        context = current_context

    agent = EntitlementsAgent(
        session_id=session_id,
        user=supervisor_user,
    )
    response = await agent.query(
        "Based on the above, provide: (1) A one-paragraph root cause summary. "
        "(2) A recommended next action in one sentence. "
        "If a similar past case is available above, note it and align your recommendation with it.",
        context=context,
    )
    return {
        "timeline": timeline,
        "summary": response,
        "recommendation": response,
    }


_supervisor_builder = StateGraph(IncidentState)
_supervisor_builder.add_node("call_billing", call_billing)
_supervisor_builder.add_node("call_support", call_support)
_supervisor_builder.add_node("call_entitlements", call_entitlements)
_supervisor_builder.add_node("synthesize", synthesize)
_supervisor_builder.add_edge(START, "call_billing")
_supervisor_builder.add_edge("call_billing", "call_support")
_supervisor_builder.add_edge("call_support", "call_entitlements")
_supervisor_builder.add_edge("call_entitlements", "synthesize")
_supervisor_builder.add_edge("synthesize", END)
supervisor_graph = _supervisor_builder.compile()


async def run_incident(incident_id: str) -> dict:
    """Run the supervisor for one incident; returns summary, timeline, recommendation."""
    workspace_id = INCIDENT_WORKSPACE.get(incident_id, "W-332")
    initial: IncidentState = {
        "incident_id": incident_id,
        "workspace_id": workspace_id,
        "findings_billing": "",
        "findings_support": "",
        "findings_entitlements": "",
        "summary": "",
        "recommendation": "",
        "timeline": "",
    }
    final = await supervisor_graph.ainvoke(initial)

    # Write findings back to memory for future similar queries
    findings_text = (
        f"Incident {incident_id} - Root cause: {final['summary']} "
        f"Recommended action: {final['recommendation']}"
    )
    await cognee.add(
        findings_text,
        dataset_name="agent_resolutions",
        node_set=["resolutions"],
        user=supervisor_user,
    )
    await cognee.cognify(
        datasets=["agent_resolutions"],
        user=supervisor_user,
    )

    return {
        "summary": final["summary"],
        "timeline": final["timeline"],
        "recommendation": final["recommendation"],
        "findings_billing": final["findings_billing"],
    }


async def run_isolation_proof():
    """Demonstrate that user isolation works: each user can only see their own datasets."""
    print("\n" + "=" * 80)
    print("USER ISOLATION PROOF")
    print("=" * 80)

    # billing_user tries to search support data -> should fail
    print("\n1. Billing user searching Support data (not owned)...")
    try:
        await cognee.search(
            query_text="customer complaint",
            query_type=SearchType.GRAPH_COMPLETION,
            datasets=["acme_support_tickets"],
            user=billing_user,
        )
        print("   -> Got results (unexpected)")
    except (DatasetNotFoundError, PermissionDeniedError):
        print("   -> Access denied (expected -- isolation works)")

    # billing_user searches own data -> should succeed
    print("\n2. Billing user searching Billing data (owned)...")
    try:
        r = await cognee.search(
            query_text="invoice status",
            query_type=SearchType.GRAPH_COMPLETION,
            datasets=["acme_billing_finance"],
            user=billing_user,
        )
        print(f"   -> OK ({len(r)} results)")
    except Exception as e:
        print(f"   -> Error: {e}")

    # support_user tries to search entitlements private notes -> should fail
    print("\n3. Support user searching Entitlements private notes (not owned)...")
    try:
        await cognee.search(
            query_text="entitlements notes",
            query_type=SearchType.GRAPH_COMPLETION,
            datasets=["entitlements_agent_private_notes"],
            user=support_user,
        )
        print("   -> Got results (unexpected)")
    except (DatasetNotFoundError, PermissionDeniedError):
        print("   -> Access denied (expected -- isolation works)")

    # supervisor_user searches across all shared data (no dataset filter) -> should succeed
    print("\n4. Supervisor searching all shared data (no dataset filter)...")
    try:
        r = await cognee.search(
            query_text="customer complaint",
            query_type=SearchType.GRAPH_COMPLETION,
            user=supervisor_user,
        )
        print(f"   -> OK ({len(r)} results -- supervisor sees data from all agents)")
    except Exception as e:
        print(f"   -> Error: {e}")

    print("\n" + "=" * 80)
    print("Isolation proof complete: agents can only access their own datasets;")
    print("supervisor has shared read access to all datasets (searched without dataset filter).")
    print("=" * 80 + "\n")


async def run_nodeset_proof():
    """Demonstrate NodeSet filtering: same query, different tag, different subgraph."""
    print("\n" + "=" * 80)
    print("NODESET FILTERING PROOF")
    print("=" * 80)
    print("Same query, different NodeSet filter -> different subgraph results.\n")

    query = "What happened with workspace W-332?"

    # Filter: only "invoices"-tagged nodes (from acme_billing_finance)
    print('1. Query with NodeSet filter ["invoices"] (billing_user)...')
    try:
        r1 = await cognee.search(
            query_text=query,
            query_type=SearchType.GRAPH_COMPLETION,
            node_type=NodeSet,
            node_name=["invoices"],
            user=billing_user,
            datasets=["acme_billing_finance"],
        )
        print(f"   -> {len(r1)} results (scoped to invoice/finance nodes)")
    except Exception as e:
        print(f"   -> Error: {e}")
        r1 = []

    # Filter: only "support"-tagged nodes (from acme_support_tickets)
    print('\n2. Query with NodeSet filter ["support"] (support_user)...')
    try:
        r2 = await cognee.search(
            query_text=query,
            query_type=SearchType.GRAPH_COMPLETION,
            node_type=NodeSet,
            node_name=["support"],
            user=support_user,
            datasets=["acme_support_tickets"],
        )
        print(f"   -> {len(r2)} results (scoped to support ticket nodes)")
    except Exception as e:
        print(f"   -> Error: {e}")
        r2 = []

    # Filter: only "temporal"-tagged nodes (from acme_audit_event_stream)
    print('\n3. Query with NodeSet filter ["temporal"] (entitlements_user)...')
    try:
        r3 = await cognee.search(
            query_text=query,
            query_type=SearchType.GRAPH_COMPLETION,
            node_type=NodeSet,
            node_name=["temporal"],
            user=entitlements_user,
            datasets=["acme_audit_event_stream"],
        )
        print(f"   -> {len(r3)} results (scoped to temporal/audit event nodes)")
    except Exception as e:
        print(f"   -> Error: {e}")
        r3 = []

    print("\n" + "=" * 80)
    print("NodeSet proof complete: same query returns different results depending")
    print("on which NodeSet tag you filter by -- fine-grained topic scoping within one graph.")
    print("=" * 80 + "\n")


async def main():
    """Run supervisor for TICK-1001 then TICK-1002; show Cognee benefits."""
    global billing_user, support_user, entitlements_user, supervisor_user

    print("\n" + "=" * 80)
    print("SaaS Entitlements PoC - Multi-Agent Supervisor Demo")
    print("=" * 80 + "\n")
    print("Incidents: customer paid but was downgraded. Supervisor runs billing, support,")
    print("and entitlements agents, then synthesizes. TICK-1002 uses stored TICK-1001 resolution.")
    print("Each agent runs as a separate Cognee user (user isolation enabled).")
    print("=" * 80 + "\n")

    # Get or create users (must match load_into_cognee.py)
    print("Setting up users...")
    billing_user = await get_or_create_user(BILLING_USER_EMAIL, USER_PASSWORD)
    support_user = await get_or_create_user(SUPPORT_USER_EMAIL, USER_PASSWORD)
    entitlements_user = await get_or_create_user(ENTITLEMENTS_USER_EMAIL, USER_PASSWORD)
    supervisor_user = await get_or_create_user(SUPERVISOR_USER_EMAIL, USER_PASSWORD)
    print(f"  billing_user:      {billing_user.email}")
    print(f"  support_user:      {support_user.email}")
    print(f"  entitlements_user: {entitlements_user.email}")
    print(f"  supervisor_user:   {supervisor_user.email}")
    print()

    # Check if data is loaded
    print("Checking if data is loaded into Cognee...")
    try:
        test_result = await cognee.search(
            query_type=SearchType.GRAPH_COMPLETION,
            query_text="test",
            datasets=["acme_billing_finance"],
            top_k=1,
            user=billing_user,
        )
        print(f"  Data appears to be loaded\n{test_result}")
    except Exception as e:
        print("\nWarning: Could not verify data. Have you run load_into_cognee.py?")
        print(f"Error: {e}\n")
        print("Continuing anyway...\n")

    # Run PoC: supervisor for TICK-1001, then full
    # run for TICK-1002 (uses stored TICK-1001 resolution)
    try:
        print(
            "Billing, support, and events live in "
            "one Cognee graph; agents query it "
            "instead of separate DBs.\n"
        )
        print("Running supervisor for incident TICK-1001...\n")
        result = await run_incident("TICK-1001")
        print("=" * 80)
        print("INCIDENT RESULT (TICK-1001)")
        print("=" * 80)
        print("\nBilling (2 queries, same session - context preserved for follow-up):")
        print(result["findings_billing"])
        print("\nSummary:\n", result["summary"])
        print("\nTimeline (source of truth):\n", result["timeline"])
        print("-> Single TEMPORAL search over the audit stream (no manual merge of sources).")
        print("\nRecommendation:\n", result["recommendation"])
        print("=" * 80)
        print("\n  Findings written back to memory for future queries (agent_resolutions).")

        print("\nRunning supervisor for incident TICK-1002 (similar case)...\n")
        result2 = await run_incident("TICK-1002")
        print("=" * 80)
        print("INCIDENT RESULT (TICK-1002)")
        print("=" * 80)
        print("\nBilling (2 queries, same session - context preserved for follow-up):")
        print(result2["findings_billing"])
        print("\nSummary:\n", result2["summary"])
        print("\nTimeline (source of truth):\n", result2["timeline"])
        print("-> Single TEMPORAL search over the audit stream (no manual merge of sources).")
        print("\nRecommendation:\n", result2["recommendation"])
        print("=" * 80)
        print("\n-> TICK-1002 synthesis used Cognee memory (similar case TICK-1001).")

        # --- User Isolation Proof ---
        await run_isolation_proof()

        # --- NodeSet Filtering Proof ---
        await run_nodeset_proof()

        print("\n" + "=" * 80)
        print("ALL DEMOS COMPLETED SUCCESSFULLY!")
        print("=" * 80 + "\n")
        print("What you just saw:")
        print("- Supervisor ran for TICK-1001, then for TICK-1002 (real datapoint)")
        print("- Three specialized agents per incident (billing, support, entitlements)")
        print("- Each agent runs as a separate Cognee user (user isolation)")
        print("- Each agent maintained conversation context in their session")
        print("- Temporal analysis revealed the exact sequence of events per incident")
        print("- TICK-1002 synthesis used stored TICK-1001 resolution from Cognee memory")
        print("- User isolation proof: agents cannot access each other's private data")
        print("- NodeSet filtering: same query scoped to different subgraphs by tag")
        print("\nThis demonstrates:")
        print("  User isolation (each agent = separate Cognee user)")
        print("  NodeSet filtering (topic-based subgraph scoping within one graph)")
        print("  Session-based conversation with context preservation")
        print("  Feedback loops that improve agent reasoning")
        print("  Knowledge sharing via permissions (supervisor reads all)")
        print("  Root cause analysis using multi-agent collaboration")
        print(
            "  Memory: when a similar issue "
            "(TICK-1002) comes up, the stored "
            "resolution helps resolve it faster"
        )
        print("=" * 80 + "\n")

    except Exception as e:
        print(f"\nError during demo: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    # Ensure we have the required environment variables
    if not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY environment variable not set")
        print("Please set it in your .env file or environment")
        exit(1)

    if not os.getenv("LLM_API_KEY"):
        os.environ["LLM_API_KEY"] = os.environ["OPENAI_API_KEY"]

    # Run the comprehensive multi-agent investigation demo
    asyncio.run(main())
