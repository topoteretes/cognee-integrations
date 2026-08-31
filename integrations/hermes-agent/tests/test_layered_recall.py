"""The layered per-prompt recall and the memory-hit header.

With ``dataset_ids`` + ``search_type`` in a single request the server's
``auto`` scope resolves graph-only, so cached Q&A turns, trace lessons and
distilled agent guidance never reached the prompt. The layered fan-out runs
one bounded call per scope, cheap lanes first, and renders each layer as its
own labelled block. Run standalone with ``python3 tests/test_layered_recall.py``.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _char_helpers import fake_backend, make_provider  # noqa: E402

_LAYERED = {"recall_session_layers": True, "recall_budget": 20}


def _settle(provider, timeout=5.0):
    thread = provider._prefetch_thread
    if thread is not None:
        thread.join(timeout=timeout)


def _prefetch(provider, query="q"):
    provider.queue_prefetch(query)
    _settle(provider)
    return provider.prefetch(query)


class TestLayeredFanOut(unittest.TestCase):
    def test_one_call_per_scope_cheap_lanes_first(self):
        with fake_backend() as fake:
            provider = make_provider(config=_LAYERED)
            _prefetch(provider)
            scopes = [kwargs["scope"] for kwargs in fake.kwargs_for("recall")]
        self.assertEqual(scopes, [["session"], ["trace"], ["session_context"], ["graph"]])

    def test_graph_lane_uses_hybrid_completion_and_only_context(self):
        with fake_backend() as fake:
            provider = make_provider(config=_LAYERED)
            _prefetch(provider)
            calls = fake.kwargs_for("recall")
        by_scope = {tuple(kwargs["scope"]): kwargs for kwargs in calls}
        self.assertEqual(by_scope[("graph",)]["query_type"], "HYBRID_COMPLETION")
        self.assertTrue(all(kwargs["only_context"] for kwargs in calls))
        # The session_context lane asks for the distilled agent rendering.
        self.assertEqual(by_scope[("session_context",)]["context_profile"], "agent")
        self.assertIsNone(by_scope[("session",)]["context_profile"])

    def test_every_lane_targets_the_plugin_dataset_and_session(self):
        with fake_backend() as fake:
            provider = make_provider(config=_LAYERED)
            _prefetch(provider)
            for kwargs in fake.kwargs_for("recall"):
                self.assertEqual(kwargs["datasets"], ["hermes"])
                self.assertEqual(kwargs["session_id"], "hermes_s-1")

    def test_results_are_rendered_as_labelled_blocks(self):
        with fake_backend() as fake:
            fake.results["recall"] = [{"text": "remembered", "source": "x"}]
            provider = make_provider(config={**_LAYERED, "memory_hits": False})
            out = _prefetch(provider)
        for label in ("<session_memory>", "<trace_lessons>", "<agent_guidance>", "<graph_memory>"):
            self.assertIn(label, out)
        self.assertIn("</session_memory>", out)

    def test_a_failing_lane_does_not_discard_the_others(self):
        with fake_backend() as fake:
            calls = {"n": 0}
            original = fake.recall

            def flaky(**kwargs):
                calls["n"] += 1
                if kwargs["scope"] == ["trace"]:
                    raise RuntimeError("trace lane down")
                original(**kwargs)
                return [{"text": "kept"}]

            fake.recall = flaky
            provider = make_provider(config={**_LAYERED, "memory_hits": False})
            out = _prefetch(provider)
        self.assertIn("<session_memory>", out)
        self.assertIn("<graph_memory>", out)
        self.assertNotIn("<trace_lessons>", out)

    def test_empty_lanes_leave_nothing_cached(self):
        with fake_backend() as fake:
            fake.results["recall"] = []
            provider = make_provider(config=_LAYERED)
            self.assertEqual(_prefetch(provider), "")

    def test_a_graph_404_is_benign_not_a_breaker_failure(self):
        # A dataset nobody has cognified answers the graph scope with 404 on
        # every prompt of a fresh install; that must not feed the breaker.
        class _Http404(RuntimeError):
            status = 404

        with fake_backend() as fake:
            original = fake.recall

            def not_built(**kwargs):
                original(**kwargs)
                if kwargs["scope"] == ["graph"]:
                    raise _Http404("graph not built")
                return []

            fake.recall = not_built
            provider = make_provider(config=_LAYERED)
            _prefetch(provider)
        self.assertEqual(provider._consecutive_failures, 0)

    def test_all_lanes_failing_counts_one_breaker_failure(self):
        with fake_backend() as fake:
            fake.errors["recall"] = RuntimeError("down")
            provider = make_provider(config=_LAYERED)
            _prefetch(provider)
        self.assertEqual(provider._consecutive_failures, 1)

    def test_zero_budget_skips_every_lane(self):
        with fake_backend() as fake:
            provider = make_provider(config={**_LAYERED, "recall_budget": 0})
            _prefetch(provider)
            self.assertEqual(fake.kwargs_for("recall"), [])


class TestCodeLane(unittest.TestCase):
    def test_configured_code_dataset_arms_the_lane_on_identifiers(self):
        with fake_backend() as fake:
            provider = make_provider(config={**_LAYERED, "code_datasets": "codebase-svc-abc123"})
            provider.queue_prefetch("what calls process_payment?")
            _settle(provider)
            scopes = [tuple(kwargs["scope"]) for kwargs in fake.kwargs_for("recall")]
            self.assertIn(("code",), scopes)
            code_call = next(
                kwargs for kwargs in fake.kwargs_for("recall") if kwargs["scope"] == ["code"]
            )
        self.assertEqual(code_call["datasets"], ["codebase-svc-abc123"])
        self.assertEqual(code_call["code_query"]["operation"], "query_facts")
        self.assertEqual(code_call["code_query"]["name"], "process_payment")

    def test_conversational_prompts_never_arm_the_code_lane(self):
        with fake_backend() as fake:
            provider = make_provider(config={**_LAYERED, "code_datasets": "codebase-svc-abc123"})
            provider.queue_prefetch("how are you today")
            _settle(provider)
            scopes = [tuple(kwargs["scope"]) for kwargs in fake.kwargs_for("recall")]
        self.assertNotIn(("code",), scopes)

    def test_code_graph_recall_off_disables_the_lane(self):
        with fake_backend() as fake:
            provider = make_provider(
                config={
                    **_LAYERED,
                    "code_datasets": "codebase-svc-abc123",
                    "code_graph_recall": False,
                }
            )
            provider.queue_prefetch("what calls process_payment?")
            _settle(provider)
            scopes = [tuple(kwargs["scope"]) for kwargs in fake.kwargs_for("recall")]
        self.assertNotIn(("code",), scopes)


class TestMemoryHitHeader(unittest.TestCase):
    def test_header_reports_hits_and_per_session_totals(self):
        with fake_backend() as fake:
            fake.results["recall"] = [{"text": "remembered"}]
            provider = make_provider(config={**_LAYERED, "memory_hits": True})
            out = _prefetch(provider)
        self.assertIn("4 memory hits this turn", out)
        self.assertIn("(2 beyond this session)", out)  # agent_guidance + graph
        self.assertIn("1/1 turns had hits this session", out)

    def test_totals_accumulate_across_turns(self):
        with fake_backend() as fake:
            fake.results["recall"] = []
            provider = make_provider(config={**_LAYERED, "memory_hits": True})
            self.assertEqual(_prefetch(provider), "")  # turn 1: no hits
            fake.results["recall"] = [{"text": "remembered"}]
            out = _prefetch(provider)  # turn 2: hits
        self.assertIn("1/2 turns had hits this session", out)

    def test_reset_session_switch_clears_the_totals(self):
        with fake_backend() as fake:
            fake.results["recall"] = [{"text": "remembered"}]
            provider = make_provider(config={**_LAYERED, "memory_hits": True})
            _prefetch(provider)
            provider.on_session_switch("s-2", reset=True)
        self.assertEqual(provider._turns_seen, 0)
        self.assertEqual(provider._hits_total, 0)

    def test_header_is_absent_when_disabled(self):
        with fake_backend() as fake:
            fake.results["recall"] = [{"text": "remembered"}]
            provider = make_provider(config={**_LAYERED, "memory_hits": False})
            out = _prefetch(provider)
        self.assertNotIn("memory hit", out)


class TestSessionScopeMapping(unittest.TestCase):
    def test_tool_session_scope_covers_the_three_session_layers(self):
        with fake_backend() as fake:
            provider = make_provider(config={"recall_session_layers": True})
            provider.handle_tool_call("cognee_recall", {"query": "q", "scope": "session"})
            kwargs = fake.only_call("recall")
        self.assertEqual(kwargs["scope"], ["session", "trace", "session_context"])
        self.assertEqual(kwargs["datasets"], ["hermes"])

    def test_legacy_session_scope_with_layers_off(self):
        with fake_backend() as fake:
            provider = make_provider(config={"recall_session_layers": False})
            provider.handle_tool_call("cognee_recall", {"query": "q", "scope": "session"})
            kwargs = fake.only_call("recall")
        self.assertEqual(kwargs["scope"], "session")
        self.assertIsNone(kwargs["datasets"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
