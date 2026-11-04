"""Validate documented CODE requests against cognee 1.6.3's wire contract.

Operations and diagram values are defined in code_retriever.CodeRetriever.
These checks catch malformed copy/paste examples without importing a server.
"""

import json
import re
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[3] / "claude-code/skills"


def test_documented_architecture_queries():
    text = (SKILLS / "cognee-architecture/SKILL.md").read_text()
    queries = [json.loads(raw) for raw in re.findall(r"--code-query '([^']+)'", text)]
    assert {q["operation"] for q in queries} == {
        "architecture",
        "insights",
        "impact_analysis",
        "delta",
    }
    for query in queries:
        assert set(query) <= {"operation", "diagram", "targets"}
        if "diagram" in query:
            assert query["diagram"] in {"mermaid", "dot"}
        if query["operation"] == "impact_analysis":
            assert isinstance(query["targets"], list) and query["targets"]
            assert all(isinstance(target, str) and target.strip() for target in query["targets"])
