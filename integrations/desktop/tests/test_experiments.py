"""Latent features: temporal detection, feedback, conversation threads."""

from pathlib import Path

from desktop_backend.experiments import ConversationThreads, is_temporal, record_feedback


def test_temporal_detection():
    assert is_temporal("what did we decide in June?")
    assert is_temporal("when did the loyalty work start")
    assert is_temporal("changes since last week")
    assert not is_temporal("who are our main competitors")


class RememberingAdapter:
    def __init__(self):
        self.remembered = []

    async def remember(self, text, *, filename="", node_set=""):
        self.remembered.append((text, node_set))


async def test_feedback_reinforces_on_positive(tmp_path):
    adapter = RememberingAdapter()
    outcome = await record_feedback(adapter, tmp_path, "q?", "the answer", 5)
    assert outcome == "reinforced"
    assert adapter.remembered and adapter.remembered[0][1] == "feedback"
    assert "user-confirmed" in adapter.remembered[0][0]
    outcome = await record_feedback(adapter, tmp_path, "q?", "bad answer", 1)
    assert outcome == "logged"
    assert len(adapter.remembered) == 1  # negative ratings do not reinforce
    assert (Path(tmp_path) / "feedback.jsonl").read_text().count("\n") == 2


def test_conversation_threads_contextualize():
    threads = ConversationThreads()
    assert threads.contextualize("t1", "and in the north?") == "and in the north?"
    threads.remember_turn("t1", "who are our competitors?", "StayFinder and RoomRover.")
    followup = threads.contextualize("t1", "and in the north region?")
    assert "who are our competitors?" in followup
    assert "Follow-up question: and in the north region?" in followup


class GraphAdapter:
    """Tenant double: one dataset whose graph has a conflicts_with edge."""

    exclude_datasets: set = set()

    class _Response:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    async def _request(self, method, path, **kwargs):
        if path == "/api/v1/datasets":
            return self._Response([{"name": "agent_sessions", "id": "ds1"}])
        return self._Response(
            {
                "nodes": [
                    {"id": "n1", "label": "websockets version conflict"},
                    {"id": "n2", "label": "targeted code inspection"},
                ],
                # cloud tenants label these conflicts_with, not contradicts
                "edges": [{"source": "n1", "target": "n2", "label": "conflicts_with"}],
            }
        )


async def test_contradictions_match_conflicts_with_edges():
    from desktop_backend.experiments import _contradiction_cache, contradictions_for

    _contradiction_cache.clear()
    hits = await contradictions_for(GraphAdapter(), "the websockets upgrade")
    assert hits and hits[0]["a"] == "websockets version conflict"
    assert hits[0]["relation"] == "conflicts_with"
    # unrelated query terms match nothing
    _contradiction_cache.clear()
    assert await contradictions_for(GraphAdapter(), "quarterly revenue") == []
