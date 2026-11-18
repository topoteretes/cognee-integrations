"""What the prompt hook injects when recall is auto vs pinned (SDK-1393).

Auto recall names no dataset and lets the server pick the dataset(s) to read,
so nothing the hook injects may claim the memory came from the launch's active
dataset. Pinned recall reads that dataset only, so a credit that names a source
names it.

The test drives the hook's real ``auto_recall_active`` and its real
``recall_via_http`` (the request body is captured at the HTTP seam), so the
request and the injected text come from the same run.

Contract, for every hook-family suite:
  * auto (nothing pinned): the graph request carries no
    ``datasets``/``dataset_ids`` and no session id, shared-memory ids included,
    while the recalled item still reaches the context;
  * pinned (COGNEE_PLUGIN_RECALL_MODE=pinned): the graph request names the
    active dataset's ids; a configured COGNEE_PLUGIN_DATASET alone stays auto;
  * the memory header (``Cognee memory: …``) never names a dataset;
  * claude-code and codex mark each item with the dataset the server names
    (none here, so its origin) and ask for a credit naming the dataset, with no
    ``[mN]`` tag; Antigravity marks nothing;
  * Cursor's credit line names its source: ``based on the <dataset> dataset``
    when pinned, ``based on Cognee memory`` when routed, matching what
    ``cognee-search.sh`` prints on its auto path.
"""

from __future__ import annotations

import sys

import pytest
from utils.recall import drive_recall, load_lookup

DATASET = "research_notes"
WRITE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"
FACT = "the retry policy backs off exponentially with a 30s ceiling"
GRAPH_ITEM = {"source": "graph", "text": f"## Relevant passages\n{FACT}."}

#: (COGNEE_PLUGIN_RECALL_MODE, expected mode); COGNEE_PLUGIN_DATASET is set in both
MODES = {
    "auto": ("", "auto"),
    "pinned_configured": ("pinned", "pinned"),
}


def _drive(suite, hook_module, monkeypatch, tmp_path, mode: str):
    configured, expected = MODES[mode]
    module = load_lookup(suite, hook_module, monkeypatch)
    # The isolated sibling the hook imported (Antigravity binds names only).
    pc = sys.modules["_plugin_common"]
    # After loading: the harness scrubs COGNEE_* from the environment.
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "on")  # Codex and Cursor start off
    monkeypatch.delenv("COGNEE_PLUGIN_READ_DATASET_IDS", raising=False)
    # A configured dataset names where saves go; only the recall mode pins.
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", DATASET)
    if configured:
        monkeypatch.setenv("COGNEE_PLUGIN_RECALL_MODE", configured)
    else:
        monkeypatch.delenv("COGNEE_PLUGIN_RECALL_MODE", raising=False)
    monkeypatch.setattr(module, "get_dataset", lambda config: DATASET)
    monkeypatch.setattr(
        module, "resolve_active_dataset_ids", lambda *a, **k: (WRITE_ID, [WRITE_ID, OTHER_ID])
    )

    sent: list[dict] = []

    def fake_http(path, payload, timeout=None, **kw):
        sent.append(dict(payload))
        return [dict(GRAPH_ITEM)] if payload.get("scope") == ["graph"] else []

    monkeypatch.setattr(pc, "_json_http_request", fake_http)
    real_recall = module.recall_via_http
    modes: list[bool] = []

    def real_auto(*a, **k):
        modes.append(pc.auto_recall_active(*a, **k))
        return modes[-1]

    run = drive_recall(
        module,
        monkeypatch,
        recall=lambda prompt, **kw: real_recall(prompt, **kw),
        auto_route=real_auto,
    )
    assert modes == [expected == "auto"], "the hook asks for the recall mode once"
    (graph,) = [p for p in sent if p.get("scope") == ["graph"]]
    context = run.output["hookSpecificOutput"]["additionalContext"]
    return graph, context


@pytest.mark.parametrize("mode", list(MODES))
def test_what_the_hook_sends_and_injects_follows_the_recall_mode(
    suite, hook_module, monkeypatch, tmp_path, mode
):
    graph, context = _drive(suite, hook_module, monkeypatch, tmp_path, mode)
    auto = MODES[mode][1] == "auto"
    if auto:
        assert "datasets" not in graph and "dataset_ids" not in graph, graph
        assert "session_id" not in graph
    else:
        assert graph["dataset_ids"] == [WRITE_ID, OTHER_ID], graph
    # Routed or not, what the server returned is rendered.
    assert FACT in context

    # The memory header (the Codex-derived cores put a status line above it).
    header = next(line for line in context.splitlines() if line.startswith("Cognee memory:"))
    assert DATASET not in header and "dataset" not in header.lower(), header

    if suite.name == "antigravity":
        # No credit tagging on this host: the memory goes in bare.
        assert "Crediting Cognee memory" not in context and "[m1" not in context
        assert DATASET not in context
        return
    assert "Crediting Cognee memory" in context
    if suite.name == "claude-code":
        # Items are marked with where they came from, not numbered (SDK-1393),
        # and the braille mark stands in for the brain under the answer.
        assert "> Cognee memory: <what you remembered>, from the <dataset> dataset" in context
        assert "[m1" not in context and "\U0001f9e0" not in context
        return
    if suite.name == "codex":
        # Marked as on Claude Code (SDK-1393); the agent writes the braille
        # logotype itself, Codex having no way to draw it under the answer.
        assert "`⡏⠁⡏⡇⡏⡁⡧⣸⢸⠅⡯`  <what you remembered>," in context
        assert "from the <dataset> dataset" in context and "<document>" not in context
        assert "[m1" not in context and "\U0001f9e0" not in context
        return
    # Cursor: the credit line says where the memory came from.
    assert suite.name == "cursor", f"{suite.name}: add its credit wording here"
    if auto:
        assert "based on Cognee memory." in context
        assert DATASET not in context
    else:
        assert f"based on the {DATASET} dataset." in context
