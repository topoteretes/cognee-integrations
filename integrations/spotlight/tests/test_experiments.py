"""Latent features: temporal detection, feedback, conversation threads."""

from pathlib import Path

from spotlight_backend.experiments import ConversationThreads, is_temporal, record_feedback


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
