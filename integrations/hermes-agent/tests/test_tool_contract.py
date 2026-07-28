"""Characterization: the model-visible tool contract and the request payloads.

Hermes hands ``handle_tool_call``'s return string straight to the model, so the
JSON envelope shapes here are a behavioural contract, not an implementation
detail. The recorded cognee kwargs are the other half of the contract: they are
what the HTTP request bodies must reproduce field for field.

Every test in this module must keep passing, unchanged, after the transport
swap. Run standalone with ``python3 tests/test_tool_contract.py``.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _char_helpers import (  # noqa: E402
    RememberResultStub,
    fake_cognee,
    make_provider,
)


def _call(provider, tool, args):
    return json.loads(provider.handle_tool_call(tool, args))


class _ModelDumpItem:
    """A pydantic-style result object (``model_dump`` returning a dict)."""

    def __init__(self, payload):
        self._payload = payload

    def model_dump(self):
        return self._payload


# --------------------------------------------------------------------------
# Envelopes
# --------------------------------------------------------------------------


class TestRecallEnvelope(unittest.TestCase):
    def test_hit_returns_results_and_count(self):
        with fake_cognee() as fake:
            fake.results["recall"] = [{"text": "alpha"}, {"text": "beta"}]
            provider = make_provider()
            out = _call(provider, "cognee_recall", {"query": "q"})
        self.assertEqual(out["count"], 2)
        self.assertEqual([item["text"] for item in out["results"]], ["alpha", "beta"])

    def test_miss_returns_result_message_and_zero_count(self):
        with fake_cognee() as fake:
            fake.results["recall"] = []
            provider = make_provider()
            out = _call(provider, "cognee_recall", {"query": "q"})
        self.assertEqual(out, {"result": "No relevant Cognee memory found.", "count": 0})

    def test_missing_query_is_an_error_and_calls_nothing(self):
        with fake_cognee() as fake:
            provider = make_provider()
            out = _call(provider, "cognee_recall", {})
        self.assertEqual(out, {"error": "Missing required parameter: query"})
        self.assertEqual(fake.kwargs_for("recall"), [])

    def test_blank_query_is_an_error(self):
        with fake_cognee():
            provider = make_provider()
            out = _call(provider, "cognee_recall", {"query": "   "})
        self.assertEqual(out, {"error": "Missing required parameter: query"})

    def test_backend_failure_is_reported_as_error_text(self):
        with fake_cognee() as fake:
            fake.errors["recall"] = RuntimeError("boom")
            provider = make_provider()
            out = _call(provider, "cognee_recall", {"query": "q"})
        self.assertTrue(out["error"].startswith("Cognee recall failed:"))
        self.assertIn("boom", out["error"])


class TestRememberEnvelope(unittest.TestCase):
    def test_success_envelope(self):
        with fake_cognee() as fake:
            fake.results["remember"] = RememberResultStub("running")
            provider = make_provider()
            out = _call(provider, "cognee_remember", {"content": "fact"})
        self.assertEqual(out, {"result": "Content stored in Cognee.", "status": "running"})

    def test_status_falls_back_to_completed_without_a_status_attribute(self):
        # NOTE for the HTTP swap: the SDK returns a RememberResult *object*, so
        # ``getattr(result, "status", ...)`` picks up the real status. An HTTP
        # backend returns a *dict*, which has no ``.status`` attribute — so it
        # would silently always report "completed". The HTTP backend must either
        # return a status-bearing object or the handler must read dict keys too.
        with fake_cognee() as fake:
            fake.results["remember"] = {"status": "running"}
            provider = make_provider()
            out = _call(provider, "cognee_remember", {"content": "fact"})
        self.assertEqual(out["status"], "completed")

    def test_missing_content_is_an_error_and_calls_nothing(self):
        with fake_cognee() as fake:
            provider = make_provider()
            out = _call(provider, "cognee_remember", {})
        self.assertEqual(out, {"error": "Missing required parameter: content"})
        self.assertEqual(fake.kwargs_for("remember"), [])

    def test_backend_failure_is_reported_as_error_text(self):
        with fake_cognee() as fake:
            fake.errors["remember"] = RuntimeError("nope")
            provider = make_provider()
            out = _call(provider, "cognee_remember", {"content": "fact"})
        self.assertTrue(out["error"].startswith("Cognee remember failed:"))


class TestForgetEnvelope(unittest.TestCase):
    def test_requires_dataset_or_everything(self):
        with fake_cognee() as fake:
            provider = make_provider()
            out = _call(provider, "cognee_forget", {})
        self.assertEqual(out, {"error": "Specify dataset or set everything=true."})
        self.assertEqual(fake.kwargs_for("forget"), [])

    def test_success_envelope_passes_backend_details_through(self):
        with fake_cognee() as fake:
            fake.results["forget"] = {"deleted": 3}
            provider = make_provider()
            out = _call(provider, "cognee_forget", {"dataset": "hermes"})
        self.assertEqual(out, {"result": "Cognee memory deleted.", "details": {"deleted": 3}})

    def test_backend_failure_is_reported_as_error_text(self):
        with fake_cognee() as fake:
            fake.errors["forget"] = RuntimeError("denied")
            provider = make_provider()
            out = _call(provider, "cognee_forget", {"everything": True})
        self.assertTrue(out["error"].startswith("Cognee forget failed:"))


class TestDispatch(unittest.TestCase):
    def test_unknown_tool_names_the_tool(self):
        with fake_cognee():
            provider = make_provider()
            out = _call(provider, "cognee_nope", {})
        self.assertEqual(out, {"error": "Unknown Cognee tool: cognee_nope"})

    def test_open_breaker_short_circuits_every_tool_without_calling_the_backend(self):
        with fake_cognee() as fake:
            provider = make_provider()
            provider._is_breaker_open = lambda: True
            for tool, args in (
                ("cognee_recall", {"query": "q"}),
                ("cognee_remember", {"content": "c"}),
                ("cognee_forget", {"everything": True}),
            ):
                out = _call(provider, tool, args)
                self.assertIn("temporarily unavailable", out["error"])
        self.assertEqual(fake.calls, [])

    def test_tool_schemas_are_the_three_declared_tools_with_required_params(self):
        provider = make_provider()
        schemas = {schema["name"]: schema for schema in provider.get_tool_schemas()}
        self.assertEqual(
            set(schemas),
            {"cognee_recall", "cognee_remember", "cognee_forget"},
        )
        self.assertEqual(schemas["cognee_recall"]["parameters"]["required"], ["query"])
        self.assertEqual(schemas["cognee_remember"]["parameters"]["required"], ["content"])
        self.assertEqual(schemas["cognee_forget"]["parameters"]["required"], [])
        self.assertEqual(
            set(schemas["cognee_recall"]["parameters"]["properties"]),
            {"query", "scope", "search_type", "top_k"},
        )


# --------------------------------------------------------------------------
# Result normalization — decides what text the model actually sees
# --------------------------------------------------------------------------


class TestResultNormalization(unittest.TestCase):
    def _first(self, item):
        with fake_cognee() as fake:
            fake.results["recall"] = [item]
            provider = make_provider()
            out = _call(provider, "cognee_recall", {"query": "q"})
        return out["results"][0]

    def test_answer_wins_over_text_and_content(self):
        item = self._first({"answer": "A", "text": "T", "content": "C", "summary": "S"})
        self.assertEqual(item["text"], "A")

    def test_text_wins_over_content(self):
        self.assertEqual(self._first({"text": "T", "content": "C"})["text"], "T")

    def test_content_wins_over_chunk_text(self):
        self.assertEqual(self._first({"content": "C", "chunk_text": "K"})["text"], "C")

    def test_chunk_text_wins_over_summary(self):
        self.assertEqual(self._first({"chunk_text": "K", "summary": "S"})["text"], "K")

    def test_summary_is_the_last_resort_key(self):
        self.assertEqual(self._first({"summary": "S"})["text"], "S")

    def test_model_dump_objects_are_coerced(self):
        item = self._first(_ModelDumpItem({"answer": "from-model-dump"}))
        self.assertEqual(item["text"], "from-model-dump")

    def test_plain_string_results_become_text(self):
        self.assertEqual(self._first("bare string")["text"], "bare string")

    def test_source_defaults_to_cognee(self):
        self.assertEqual(self._first({"text": "T"})["source"], "cognee")

    def test_source_prefers_source_then_underscore_source(self):
        self.assertEqual(self._first({"text": "T", "source": "graph"})["source"], "graph")
        self.assertEqual(self._first({"text": "T", "_source": "session"})["source"], "session")

    def test_optional_metadata_included_only_when_present(self):
        rich = self._first(
            {
                "text": "T",
                "score": 0.5,
                "dataset": "d",
                "dataset_name": "dn",
                "node_name": "nn",
            }
        )
        self.assertEqual(rich["score"], 0.5)
        self.assertEqual(rich["dataset"], "d")
        self.assertEqual(rich["dataset_name"], "dn")
        self.assertEqual(rich["node_name"], "nn")

        plain = self._first({"text": "T", "score": None})
        self.assertEqual(set(plain), {"text", "source"})


# --------------------------------------------------------------------------
# Request payloads — what the HTTP bodies must reproduce
# --------------------------------------------------------------------------


class TestRecallPayload(unittest.TestCase):
    def _recall_kwargs(self, args, **provider_kwargs):
        with fake_cognee() as fake:
            provider = make_provider(**provider_kwargs)
            provider.handle_tool_call("cognee_recall", args)
            return fake.only_call("recall")

    def test_query_text_top_k_and_auto_route_always_sent(self):
        kwargs = self._recall_kwargs({"query": "hello"}, top_k=7, auto_route=False)
        self.assertEqual(kwargs["query_text"], "hello")
        self.assertEqual(kwargs["top_k"], 7)
        self.assertIs(kwargs["auto_route"], False)

    def test_auto_scope_sends_both_session_and_datasets(self):
        kwargs = self._recall_kwargs({"query": "q"}, session_cognee_id="hermes_abc")
        self.assertEqual(kwargs["session_id"], "hermes_abc")
        self.assertEqual(kwargs["datasets"], ["hermes"])

    def test_missing_scope_defaults_to_auto(self):
        kwargs = self._recall_kwargs({"query": "q", "scope": None})
        self.assertIn("session_id", kwargs)
        self.assertIn("datasets", kwargs)

    def test_session_scope_sends_session_only(self):
        kwargs = self._recall_kwargs({"query": "q", "scope": "session"})
        self.assertIn("session_id", kwargs)
        self.assertNotIn("datasets", kwargs)

    def test_graph_scope_sends_datasets_only(self):
        kwargs = self._recall_kwargs({"query": "q", "scope": "graph"})
        self.assertEqual(kwargs["datasets"], ["hermes"])
        self.assertNotIn("session_id", kwargs)

    def test_scope_is_case_insensitive(self):
        kwargs = self._recall_kwargs({"query": "q", "scope": "GRAPH"})
        self.assertNotIn("session_id", kwargs)

    def test_search_type_applied_outside_session_scope(self):
        kwargs = self._recall_kwargs({"query": "q", "scope": "graph", "search_type": "CHUNKS"})
        self.assertEqual(kwargs["query_type"], "CHUNKS")

    def test_search_type_ignored_in_session_scope(self):
        kwargs = self._recall_kwargs({"query": "q", "scope": "session", "search_type": "CHUNKS"})
        self.assertNotIn("query_type", kwargs)

    def test_unknown_search_type_falls_back_to_graph_completion(self):
        kwargs = self._recall_kwargs({"query": "q", "search_type": "NOT_A_TYPE"})
        self.assertEqual(kwargs["query_type"], "GRAPH_COMPLETION")

    def test_no_query_type_when_search_type_absent(self):
        self.assertNotIn("query_type", self._recall_kwargs({"query": "q"}))


class TestTopKClamping(unittest.TestCase):
    def _top_k(self, args, configured=5):
        with fake_cognee() as fake:
            provider = make_provider(top_k=configured)
            provider.handle_tool_call("cognee_recall", {"query": "q", **args})
            return fake.only_call("recall")["top_k"]

    def test_absent_top_k_uses_provider_default(self):
        self.assertEqual(self._top_k({}, configured=9), 9)

    def test_zero_is_falsy_and_falls_back_to_provider_default(self):
        self.assertEqual(self._top_k({"top_k": 0}, configured=9), 9)

    def test_negative_clamps_to_one(self):
        self.assertEqual(self._top_k({"top_k": -3}), 1)

    def test_above_twenty_clamps_to_twenty(self):
        self.assertEqual(self._top_k({"top_k": 50}), 20)

    def test_in_range_passes_through(self):
        self.assertEqual(self._top_k({"top_k": 12}), 12)

    def test_numeric_string_is_accepted(self):
        self.assertEqual(self._top_k({"top_k": "8"}), 8)


class TestRememberPayload(unittest.TestCase):
    def test_explicit_remember_targets_the_permanent_graph(self):
        with fake_cognee() as fake:
            provider = make_provider(session_cognee_id="hermes_s9")
            provider.handle_tool_call("cognee_remember", {"content": "durable fact"})
            kwargs = fake.only_call("remember")
        self.assertEqual(kwargs["data"], "durable fact")
        self.assertEqual(kwargs["dataset_name"], "hermes")
        self.assertIs(kwargs["self_improvement"], True)
        self.assertEqual(kwargs["session_ids"], ["hermes_s9"])
        self.assertNotIn("session_id", kwargs)

    def test_dataset_argument_overrides_the_provider_dataset(self):
        with fake_cognee() as fake:
            provider = make_provider()
            provider.handle_tool_call("cognee_remember", {"content": "c", "dataset": "other"})
            self.assertEqual(fake.only_call("remember")["dataset_name"], "other")

    def test_content_is_stripped(self):
        with fake_cognee() as fake:
            provider = make_provider()
            provider.handle_tool_call("cognee_remember", {"content": "  padded  "})
            self.assertEqual(fake.only_call("remember")["data"], "padded")


class TestForgetPayload(unittest.TestCase):
    def test_dataset_scoped_delete(self):
        with fake_cognee() as fake:
            provider = make_provider()
            provider.handle_tool_call("cognee_forget", {"dataset": "hermes"})
            kwargs = fake.only_call("forget")
        self.assertEqual(kwargs["dataset"], "hermes")
        self.assertIs(kwargs["everything"], False)
        self.assertIs(kwargs["memory_only"], False)

    def test_everything_omits_the_dataset(self):
        with fake_cognee() as fake:
            provider = make_provider()
            provider.handle_tool_call("cognee_forget", {"dataset": "hermes", "everything": True})
            kwargs = fake.only_call("forget")
        self.assertIs(kwargs["everything"], True)
        self.assertNotIn("dataset", kwargs)

    def test_memory_only_is_forwarded(self):
        with fake_cognee() as fake:
            provider = make_provider()
            provider.handle_tool_call("cognee_forget", {"dataset": "hermes", "memory_only": True})
            self.assertIs(fake.only_call("forget")["memory_only"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
