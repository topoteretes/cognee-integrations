"""The recall body, checked against cognee's own parser instead of our reading of it.

Every other test in this suite asserts the *shape* the plugin sends, using fakes,
so it never needs cognee installed. That is the right default, but it cannot
catch the failure this file exists for: a body that is well-formed, accepted with
a 2xx, and still means something other than what the plugin intended — because
the server supplies a default for a field the plugin left out.

That is exactly what happened. ``/api/v1/recall`` defaults a *missing*
``search_type`` to ``GRAPH_COMPLETION`` (deliberately, so older clients keep
their behaviour), and cognee only folds the session cache into an ``auto`` scope
while the search type is null. Omitting the key therefore cost auto-routing *and*
every session read, on every scope, with no error anywhere — the write path was
storing turns the read path could never see.

So these tests parse the real ``RecallPayloadDTO`` and call the real
``normalize_scope``: the server's own code decides what the request means. They
are skipped when cognee is not installed.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cognee_integration_hermes.http_backend import HttpBackend  # noqa: E402
from test_http_backend import FakeOpener  # noqa: E402

_HAS_COGNEE = importlib.util.find_spec("cognee") is not None
_REASON = "install cognee to check the wire contract against its own parser"


def _sent_body(**overrides):
    """The JSON the transport actually puts on the wire for a recall."""
    params = {
        "query": "q",
        "session_id": "hermes_s1",
        "datasets": ["agent_sessions"],
        "top_k": 5,
        "auto_route": True,
        "query_type": None,
        "scope": "auto",
        "timeout": 5.0,
    }
    params.update(overrides)
    opener = FakeOpener({"/api/v1/recall": []})
    backend = HttpBackend(opener=opener)
    backend.url = "http://127.0.0.1:8011"
    backend.api_key = "k"
    backend.recall(**params)
    return opener.json_body("/api/v1/recall")


@unittest.skipUnless(_HAS_COGNEE, _REASON)
class TestRecallBodyMeansWhatWeIntend(unittest.TestCase):
    def _parse(self, body):
        from cognee.api.v1.recall.routers.get_recall_router import RecallPayloadDTO

        return RecallPayloadDTO(**body)

    def _sources(self, dto):
        """cognee's source resolution for an ``auto`` scope (recall.py).

        Mirrors the server's branch rather than importing it — it is inline in
        ``recall()`` — so the assertions below lean on the two *parsed* inputs it
        reads, which is where the bug actually lived.
        """
        from cognee.memory.entries import normalize_scope

        resolved = normalize_scope(dto.scope)
        if resolved != ["auto"]:
            return resolved
        if dto.session_id and dto.search_type is None:
            return ["session", "graph"]
        return ["graph"]

    def test_the_body_is_accepted_by_the_servers_own_dto(self):
        self._parse(_sent_body())

    def test_the_default_config_reaches_the_query_classifier(self):
        # search_type must survive parsing as None. The regression was that the
        # key never arrived, so the DTO default took over.
        self.assertIsNone(self._parse(_sent_body()).search_type)

    def test_omitting_search_type_is_what_broke_auto_routing(self):
        # Characterizes the old wire format: same request minus the key, parsed
        # by the same DTO, silently means GRAPH_COMPLETION.
        body = _sent_body()
        body.pop("search_type")
        self.assertEqual(str(self._parse(body).search_type), "SearchType.GRAPH_COMPLETION")

    def test_auto_route_false_still_pins_graph_completion(self):
        dto = self._parse(_sent_body(auto_route=False))
        self.assertEqual(str(dto.search_type), "SearchType.GRAPH_COMPLETION")

    def test_every_scope_resolves_to_the_sources_the_tool_advertises(self):
        # The cognee_recall schema promises "auto, session, or graph". Before the
        # fix all three resolved to ["graph"].
        self.assertIn("session", self._sources(self._parse(_sent_body(scope="session"))))
        self.assertIn("session", self._sources(self._parse(_sent_body(scope="auto"))))
        self.assertEqual(
            self._sources(self._parse(_sent_body(scope="graph", session_id=None))), ["graph"]
        )

    def test_session_scope_survives_a_pinned_search_type(self):
        # COGNEE_AUTO_ROUTE=false must not cost the session cache: the stated
        # scope decides the sources, so it no longer rides on search_type.
        dto = self._parse(_sent_body(scope="session", auto_route=False))
        self.assertEqual(self._sources(dto), ["session"])

    def test_the_old_body_could_not_reach_the_session_cache(self):
        # The regression, end to end: strip search_type and the stated scope —
        # the 0.2.0 wire format — and no scope can see session memory.
        for scope in ("session", "auto"):
            body = _sent_body(scope=scope)
            body.pop("search_type")
            body.pop("scope")
            self.assertEqual(self._sources(self._parse(body)), ["graph"])


if __name__ == "__main__":
    unittest.main()
