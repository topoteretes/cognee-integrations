"""The per-prompt memory block and the memory-hit header.

Every prompt makes exactly one memory request — ``scope=["graph"]``,
``query_type=HYBRID_COMPLETION``, ``only_context=True``, with this
conversation's ``session_id`` — plus the deterministic code lane when it is
armed. On cognee >= 1.6.0 the graph item's ``text`` is the full LLM input the
completion would have received (history, question + retrieved context,
session guidance), so it is injected verbatim: never truncated, never parsed,
and its ``system_prompt`` field is never read. Older servers put the bare
retrieval context in ``text`` and it travels the same way. Run standalone with
``python3 tests/test_layered_recall.py``.
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _char_helpers import fake_backend, fake_cognee, make_provider  # noqa: E402
from cognee_integration_hermes.backend import SdkBackend  # noqa: E402

_LAYERED = {"recall_budget": 20}
# Arms the code lane: a configured code dataset plus an identifier in the prompt.
_WITH_CODE = {**_LAYERED, "code_datasets": "codebase-svc-abc123"}
_CODE_QUERY = "what calls process_payment?"

# A cognee >= 1.6.0 only_context completion item: ``text`` is the whole prompt
# the completion would have read, ``system_prompt`` the retriever's task
# template. Well over any line budget, with the context in the middle.
_SYSTEM_PROMPT = "Answer the question using the provided context."
_HISTORY = "User: we picked Postgres for the ledger.\nAssistant: Noted, Postgres it is.\n"
_CONTEXT = "\n".join(
    f"- ledger fact {i}: the ledger service stores balances in Postgres" for i in range(12)
)
_GUIDANCE = "\nSession guidance: prefer concise answers and cite the ledger schema."
_PROMPT_TEXT = (
    f"{_HISTORY}The question is: `what database does the ledger use?`\n"
    f"Context:\n`{_CONTEXT}`\n{_GUIDANCE}"
)
assert len(_PROMPT_TEXT) > 600
_V160_ITEM = {"source": "graph", "text": _PROMPT_TEXT, "system_prompt": _SYSTEM_PROMPT}
_OLD_SERVER_ITEM = {"source": "graph", "text": "bare context"}


def _settle(provider, timeout=5.0):
    thread = provider._prefetch_thread
    if thread is not None:
        thread.join(timeout=timeout)


def _prefetch(provider, query="q"):
    provider.queue_prefetch(query)
    _settle(provider)
    return provider.prefetch(query)


def _block(out, tag):
    """The rendered content between ``<tag>`` and ``</tag>``."""
    start = out.index(f"<{tag}>\n") + len(f"<{tag}>\n")
    end = out.index(f"\n</{tag}>")
    return out[start:end]


class TestSingleMemoryRequest(unittest.TestCase):
    def test_exactly_one_recall_per_prompt(self):
        with fake_backend() as fake:
            provider = make_provider(config=_LAYERED)
            _prefetch(provider)
            calls = fake.kwargs_for("recall")
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["scope"], ["graph"])

    def test_the_request_is_explicit_and_carries_the_session(self):
        with fake_backend() as fake:
            provider = make_provider(config=_LAYERED)
            _prefetch(provider)
            kwargs = fake.only_call("recall")
        self.assertEqual(kwargs["scope"], ["graph"])
        self.assertEqual(kwargs["query_type"], "HYBRID_COMPLETION")
        self.assertIs(kwargs["only_context"], True)
        # The server builds the history and guidance layers from session_id.
        self.assertTrue(kwargs["session_id"])
        self.assertEqual(kwargs["session_id"], "hermes_s-1")
        self.assertEqual(kwargs["datasets"], ["hermes"])
        self.assertNotIn("context_profile", kwargs)
        self.assertIsNone(kwargs["code_query"])

    def test_no_separate_session_layer_requests(self):
        with fake_backend() as fake:
            provider = make_provider(config=_LAYERED)
            _prefetch(provider)
            scopes = [tuple(kwargs["scope"]) for kwargs in fake.kwargs_for("recall")]
        for retired in (("session",), ("trace",), ("session_context",)):
            self.assertNotIn(retired, scopes)

    def test_one_memory_request_plus_code_when_armed(self):
        with fake_backend() as fake:
            provider = make_provider(config=_WITH_CODE)
            _prefetch(provider, _CODE_QUERY)
            calls = fake.kwargs_for("recall")
        self.assertEqual(sorted(tuple(c["scope"]) for c in calls), [("code",), ("graph",)])
        for kwargs in calls:
            self.assertIs(kwargs["only_context"], True)
            self.assertEqual(kwargs["session_id"], "hermes_s-1")

    def test_zero_budget_skips_the_request(self):
        with fake_backend() as fake:
            provider = make_provider(config={**_LAYERED, "recall_budget": 0})
            _prefetch(provider)
            self.assertEqual(fake.kwargs_for("recall"), [])


class TestMemoryBlockRendering(unittest.TestCase):
    def test_v160_item_is_injected_whole(self):
        with fake_backend() as fake:
            fake.results["recall"] = [_V160_ITEM]
            provider = make_provider(config={**_LAYERED, "memory_hits": False})
            out = _prefetch(provider)
        self.assertEqual(_block(out, "cognee_memory"), _PROMPT_TEXT)
        # Nothing was cut: history at the top, guidance at the end.
        self.assertIn(_HISTORY.strip(), out)
        self.assertIn(_GUIDANCE.strip(), out)
        self.assertIn("ledger fact 11", out)

    def test_system_prompt_is_never_injected(self):
        with fake_backend() as fake:
            fake.results["recall"] = [_V160_ITEM]
            provider = make_provider(config=_LAYERED)
            out = _prefetch(provider)
        self.assertNotIn(_SYSTEM_PROMPT, out)
        self.assertNotIn("system_prompt", out)

    def test_old_server_bare_context_renders(self):
        with fake_backend() as fake:
            fake.results["recall"] = [_OLD_SERVER_ITEM]
            provider = make_provider(config={**_LAYERED, "memory_hits": False})
            out = _prefetch(provider)
        self.assertEqual(out, "## Cognee Memory\n<cognee_memory>\nbare context\n</cognee_memory>")

    def test_text_is_not_truncated(self):
        with fake_backend() as fake:
            fake.results["recall"] = [{"source": "graph", "text": "x" * 5000}]
            provider = make_provider(config=_LAYERED)
            out = _prefetch(provider)
        self.assertEqual(out.count("x"), 5000)

    def test_text_wins_over_other_fields(self):
        with fake_backend() as fake:
            fake.results["recall"] = [
                {
                    "source": "graph",
                    "text": "the memory",
                    "content": "not this",
                    "chunk_text": "nor this",
                }
            ]
            provider = make_provider(config={**_LAYERED, "memory_hits": False})
            out = _prefetch(provider)
        self.assertEqual(_block(out, "cognee_memory"), "the memory")

    def test_blank_items_leave_nothing_cached(self):
        with fake_backend() as fake:
            fake.results["recall"] = [{"source": "graph", "text": "   "}]
            provider = make_provider(config=_LAYERED)
            self.assertEqual(_prefetch(provider), "")

    def test_empty_recall_leaves_nothing_cached(self):
        with fake_backend() as fake:
            fake.results["recall"] = []
            provider = make_provider(config=_LAYERED)
            self.assertEqual(_prefetch(provider), "")

    def test_code_block_precedes_memory_block_whatever_answers_first(self):
        with fake_backend() as fake:
            original = fake.recall

            def staggered(**kwargs):
                # The code lane answers last; the rendering order must not care.
                time.sleep(0.25 if kwargs["scope"] == ["code"] else 0.0)
                original(**kwargs)
                return [{"text": f"from {kwargs['scope'][0]}"}]

            fake.recall = staggered
            provider = make_provider(config={**_WITH_CODE, "memory_hits": False})
            out = _prefetch(provider, _CODE_QUERY)
        self.assertLess(out.index("<code_graph>"), out.index("<cognee_memory>"), out)
        self.assertIn("- [cognee] from code", _block(out, "code_graph"))
        self.assertEqual(_block(out, "cognee_memory"), "from graph")


class TestLaneResilience(unittest.TestCase):
    def test_lanes_run_concurrently_under_one_deadline(self):
        # Two lanes sleeping 0.3s each must cost ~0.3s, not 0.6s, and both must
        # be handed the same deadline: min(recall_timeout, budget).
        with fake_backend() as fake:
            original = fake.recall

            def slow(**kwargs):
                time.sleep(0.3)
                return original(**kwargs)

            fake.recall = slow
            provider = make_provider(config={**_WITH_CODE, "recall_timeout": 5, "recall_budget": 2})
            started = time.monotonic()
            _prefetch(provider, _CODE_QUERY)
            wall = time.monotonic() - started
            timeouts = {kwargs["timeout"] for kwargs in fake.kwargs_for("recall")}
        self.assertEqual(len(fake.kwargs_for("recall")), 2)
        self.assertLess(wall, 0.55, f"lanes ran back to back: {wall:.2f}s for 2 x 0.3s")
        self.assertEqual(len(timeouts), 1, timeouts)
        self.assertLessEqual(max(timeouts), 2.0)
        self.assertGreater(max(timeouts), 1.5)

    def test_a_failing_code_lane_does_not_discard_the_memory(self):
        with fake_backend() as fake:
            original = fake.recall

            def flaky(**kwargs):
                if kwargs["scope"] == ["code"]:
                    raise RuntimeError("code lane down")
                original(**kwargs)
                return [_OLD_SERVER_ITEM]

            fake.recall = flaky
            provider = make_provider(config={**_WITH_CODE, "memory_hits": False})
            out = _prefetch(provider, _CODE_QUERY)
        self.assertIn("<cognee_memory>", out)
        self.assertNotIn("<code_graph>", out)
        self.assertEqual(provider._consecutive_failures, 0)

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

    def test_the_memory_request_failing_counts_one_breaker_failure(self):
        with fake_backend() as fake:
            fake.errors["recall"] = RuntimeError("down")
            provider = make_provider(config=_LAYERED)
            _prefetch(provider)
        self.assertEqual(provider._consecutive_failures, 1)


class TestSdkBackendLanes(unittest.TestCase):
    """The lanes through the in-process SDK transport.

    ``SdkBackend`` hands every call to one dedicated event loop and waits on
    ``future.result(timeout)``. Lanes submitted from pool threads must
    interleave as coroutines on that loop — not queue behind each other — and a
    lane that outlives its deadline must fail alone while the loop keeps
    serving the other.
    """

    def _sdk_provider(self, config=None):
        backend = SdkBackend()
        provider = make_provider(backend=backend, config={**_LAYERED, **(config or {})})
        return backend, provider

    def test_memory_request_reaches_cognee_recall_explicitly(self):
        with fake_cognee() as fake:
            backend, provider = self._sdk_provider()
            try:
                _prefetch(provider)
                kwargs = fake.only_call("recall")
            finally:
                backend.close(unregister=False)
        self.assertEqual(kwargs["query_type"], "HYBRID_COMPLETION")
        self.assertIs(kwargs["only_context"], True)
        self.assertEqual(kwargs["scope"], ["graph"])
        self.assertEqual(kwargs["session_id"], "hermes_s-1")
        self.assertEqual(kwargs["datasets"], ["hermes"])

    def test_lanes_interleave_on_the_single_sdk_loop(self):
        with fake_cognee() as fake:
            original = fake.recall

            async def slow(**kwargs):
                await asyncio.sleep(0.3)
                return await original(**kwargs)

            sys.modules["cognee"].recall = slow
            backend, provider = self._sdk_provider({"code_datasets": "codebase-svc-abc123"})
            try:
                started = time.monotonic()
                _prefetch(provider, _CODE_QUERY)
                wall = time.monotonic() - started
                calls = fake.kwargs_for("recall")
            finally:
                backend.close(unregister=False)
        self.assertEqual(len(calls), 2, calls)
        self.assertLess(wall, 0.55, f"lanes queued behind each other on the loop: {wall:.2f}s")

    def test_a_lane_past_its_deadline_fails_alone(self):
        with fake_cognee() as fake:
            original = fake.recall

            async def memory_hangs(**kwargs):
                # Only the memory lane names a search type; the code lane
                # passes None.
                if kwargs.get("query_type") is not None:
                    await asyncio.sleep(3)
                await original(**kwargs)
                return [{"text": f"from {kwargs.get('scope')}"}]

            sys.modules["cognee"].recall = memory_hangs
            backend, provider = self._sdk_provider(
                {
                    "recall_timeout": 0.5,
                    "memory_hits": False,
                    "code_datasets": "codebase-svc-abc123",
                }
            )
            try:
                started = time.monotonic()
                out = _prefetch(provider, _CODE_QUERY)
                wall = time.monotonic() - started
            finally:
                backend.close(unregister=False)
        self.assertLess(wall, 1.5, f"the hung lane held the prefetch: {wall:.2f}s")
        self.assertIn("<code_graph>", out)
        self.assertNotIn("<cognee_memory>", out)
        # One lane timing out while the other answered is proof of life, not failure.
        self.assertEqual(provider._consecutive_failures, 0)


class TestCodeLane(unittest.TestCase):
    def test_configured_code_dataset_arms_the_lane_on_identifiers(self):
        with fake_backend() as fake:
            provider = make_provider(config=_WITH_CODE)
            provider.queue_prefetch(_CODE_QUERY)
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
            provider = make_provider(config=_WITH_CODE)
            provider.queue_prefetch("how are you today")
            _settle(provider)
            scopes = [tuple(kwargs["scope"]) for kwargs in fake.kwargs_for("recall")]
        self.assertEqual(scopes, [("graph",)])

    def test_code_graph_recall_off_disables_the_lane(self):
        with fake_backend() as fake:
            provider = make_provider(config={**_WITH_CODE, "code_graph_recall": False})
            provider.queue_prefetch(_CODE_QUERY)
            _settle(provider)
            scopes = [tuple(kwargs["scope"]) for kwargs in fake.kwargs_for("recall")]
        self.assertEqual(scopes, [("graph",)])


class TestMemoryHitHeader(unittest.TestCase):
    def test_header_reports_hits_and_per_session_totals(self):
        with fake_backend() as fake:
            fake.results["recall"] = [_V160_ITEM]
            provider = make_provider(config={**_LAYERED, "memory_hits": True})
            out = _prefetch(provider)
        self.assertIn("1 memory hit this turn", out)
        self.assertNotIn("beyond this session", out)
        self.assertIn("1/1 turns had hits this session", out)

    def test_code_hits_count_toward_the_turn_without_a_provenance_note(self):
        with fake_backend() as fake:
            fake.results["recall"] = [{"text": "remembered"}]
            provider = make_provider(config={**_WITH_CODE, "memory_hits": True})
            out = _prefetch(provider, _CODE_QUERY)
        self.assertIn("2 memory hits this turn", out)
        self.assertNotIn("beyond this session", out)

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


# The server scopes memory must never read: the session cache (cached turns,
# tool-call trace lessons, distilled guidance) and the ``auto`` scope that folds
# it in. ``None`` is the same thing by omission — the server defaults to auto.
_SESSION_SCOPES = ("session", "trace", "session_context", "auto")


def _assert_graph_only(case, calls):
    """Every recall read the graph or the code graph, and stated it outright."""
    case.assertTrue(calls, "no recall was made")
    for kwargs in calls:
        scope = kwargs.get("scope")
        case.assertIn(scope, (["graph"], ["code"]), kwargs)
        for retired in _SESSION_SCOPES:
            case.assertNotIn(retired, scope)
        case.assertNotIn("context_profile", kwargs)


class TestExplicitRecallReadsTheGraph(unittest.TestCase):
    """The ``cognee_recall`` tool targets the graph too; its old ``scope``
    argument (auto | session | graph) is gone, and a caller still passing one
    is ignored."""

    def _recall_kwargs(self, args):
        with fake_backend() as fake:
            provider = make_provider(session_cognee_id="hermes_abc")
            provider.handle_tool_call("cognee_recall", args)
            return fake.only_call("recall")

    def test_the_tool_sends_the_graph_scope_with_dataset_and_session(self):
        kwargs = self._recall_kwargs({"query": "q"})
        self.assertEqual(kwargs["scope"], ["graph"])
        self.assertEqual(kwargs["datasets"], ["hermes"])
        # The session id travels so the 1.6.0 graph item carries the history;
        # an explicit graph scope never returns raw session entries.
        self.assertEqual(kwargs["session_id"], "hermes_abc")
        self.assertIsNone(kwargs["query_type"])

    def test_a_legacy_scope_argument_never_changes_the_request(self):
        baseline = self._recall_kwargs({"query": "q"})
        for legacy in ("auto", "session", "graph", "GRAPH", "SESSION", None, ""):
            with self.subTest(scope=legacy):
                kwargs = self._recall_kwargs({"query": "q", "scope": legacy})
                self.assertEqual(kwargs, baseline)
                self.assertEqual(kwargs["scope"], ["graph"])

    def test_search_type_is_the_callers_override(self):
        kwargs = self._recall_kwargs({"query": "q", "scope": "session", "search_type": "CHUNKS"})
        self.assertEqual(kwargs["query_type"], "CHUNKS")
        self.assertEqual(kwargs["scope"], ["graph"])

    def test_the_tool_schema_has_no_scope_argument(self):
        with fake_backend():
            provider = make_provider()
            schema = next(s for s in provider.get_tool_schemas() if s["name"] == "cognee_recall")
        self.assertNotIn("scope", schema["parameters"]["properties"])
        for retired in ("session memory", "scope"):
            self.assertNotIn(retired, schema["description"].lower())


class TestNoCodePathReadsTheSessionCache(unittest.TestCase):
    """Every read path — the per-prompt prefetch with and without the code
    lane, the explicit tool under every legacy scope, and the code-search tool
    — states ``["graph"]`` or ``["code"]``. Nothing requests ``session``,
    ``trace``, ``session_context`` or ``auto``, and nothing leaves the scope to
    the server."""

    def test_every_read_path_states_graph_or_code(self):
        with fake_backend() as fake:
            provider = make_provider(config=_WITH_CODE)
            _prefetch(provider)
            _prefetch(provider, _CODE_QUERY)
            for legacy in ("auto", "session", "graph", None):
                provider.handle_tool_call("cognee_recall", {"query": "q", "scope": legacy})
            provider.handle_tool_call(
                "cognee_code_search",
                {"operation": "query_facts", "name": "process_payment", "dataset": "codebase-x"},
            )
            calls = fake.kwargs_for("recall")
        self.assertGreaterEqual(len(calls), 8, calls)
        _assert_graph_only(self, calls)

    def test_the_sdk_transport_states_graph_or_code_too(self):
        with fake_cognee() as fake:
            backend = SdkBackend()
            provider = make_provider(backend=backend, config=_WITH_CODE)
            try:
                _prefetch(provider, _CODE_QUERY)
                provider.handle_tool_call("cognee_recall", {"query": "q", "scope": "session"})
                calls = fake.kwargs_for("recall")
            finally:
                backend.close(unregister=False)
        _assert_graph_only(self, calls)

    def test_no_source_line_requests_a_session_scope(self):
        # Belt and braces for the runtime tests above: the package must not even
        # spell a session-cache request. Prose (docstrings saying what is *not*
        # requested) uses backticks, not quoted literals, so it does not match.
        import re

        forbidden = re.compile(
            r"""["']session_context["']|["']trace["']|context_profile"""
            r"""|scope=["'](auto|session)["']|\[["']session["']"""
        )
        package = ROOT / "cognee_integration_hermes"
        offenders = [
            f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
            for path in sorted(package.glob("*.py"))
            for number, line in enumerate(path.read_text().splitlines(), 1)
            if forbidden.search(line)
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
