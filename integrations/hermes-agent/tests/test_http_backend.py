"""Wire-level tests for ``HttpBackend``: protocol args in, HTTP requests out.

The mirror of ``test_sdk_backend.py`` for the direct-HTTP transport. The provider
tests already pin Hermes behaviour against the protocol; this file pins that the
protocol becomes the request bodies cognee's routers actually accept (verified
against cognee 1.2.1's DTOs).

The headline assertion is :meth:`TestImproveWireFormat.test_session_ids_reach_the_wire`
— the whole reason this transport exists.

A fake opener stands in for ``urlopen``, so nothing here touches the network.
Runs standalone with ``python3 tests/test_http_backend.py``; needs no cognee.
"""

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _char_helpers import make_provider  # noqa: E402
from cognee_integration_hermes.http_backend import (  # noqa: E402
    CogneeHttpError,
    CogneeUnreachable,
    HttpBackend,
)

_TIMEOUT = 5.0
_URL = "http://127.0.0.1:8000"
_REMOTE_URL = "https://cloud.example"


class _Response(io.BytesIO):
    def __init__(self, payload, status=200):
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        super().__init__(body.encode("utf-8") if isinstance(body, str) else body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records requests and replies from a per-path script."""

    def __init__(self, responses=None):
        self.requests = []
        self.responses = responses or {}
        self.default = {}

    def __call__(self, request, timeout=None):
        path = request.selector if hasattr(request, "selector") else request.full_url
        body = request.data
        self.requests.append(
            {
                "method": request.get_method(),
                "path": path,
                "url": request.full_url,
                "headers": {k.lower(): v for k, v in request.header_items()},
                "body": body,
                "timeout": timeout,
            }
        )
        outcome = self.responses.get(self._key(path), self.default)
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)

    @staticmethod
    def _key(path):
        return path.split("?")[0]

    # -- inspection --------------------------------------------------------

    def paths(self):
        return [r["path"] for r in self.requests]

    def request_for(self, path):
        matches = [r for r in self.requests if r["path"] == path]
        if len(matches) != 1:
            raise AssertionError(f"expected exactly 1 request to {path}, got {len(matches)}")
        return matches[0]

    def json_body(self, path):
        return json.loads(self.request_for(path)["body"].decode("utf-8"))

    def multipart_fields(self, path):
        """Parse a multipart body into ``{name: value}`` (text parts only)."""
        raw = self.request_for(path)["body"].decode("utf-8", errors="replace")
        fields = {}
        for chunk in raw.split("--")[1:]:
            if 'name="' not in chunk:
                continue
            name = chunk.split('name="', 1)[1].split('"', 1)[0]
            if "\r\n\r\n" not in chunk:
                continue
            value = chunk.split("\r\n\r\n", 1)[1]
            fields[name] = value.rsplit("\r\n", 1)[0]
        return fields


def _backend(opener, *, api_key="k", url=_URL, cache_dir=None):
    backend = HttpBackend(opener=opener, cache_dir=cache_dir)
    backend.url = url
    backend.api_key = api_key
    return backend


class TestRecallWireFormat(unittest.TestCase):
    def _recall(self, **overrides):
        params = {
            "query": "q",
            "session_id": "hermes_s1",
            "datasets": ["hermes"],
            "top_k": 5,
            "auto_route": True,
            "query_type": None,
            "timeout": _TIMEOUT,
        }
        params.update(overrides)
        opener = FakeOpener({"/api/v1/recall": [{"text": "hit"}]})
        results = _backend(opener).recall(**params)
        return opener, results

    def test_posts_to_the_recall_endpoint(self):
        opener, _ = self._recall()
        self.assertEqual(opener.request_for("/api/v1/recall")["method"], "POST")

    def test_query_is_named_query_not_query_text(self):
        # The endpoint's DTO field is ``query``; only the SDK calls it query_text.
        opener, _ = self._recall(query="hello")
        body = opener.json_body("/api/v1/recall")
        self.assertEqual(body["query"], "hello")
        self.assertNotIn("query_text", body)

    def test_session_and_datasets_are_sent_when_targeted(self):
        opener, _ = self._recall()
        body = opener.json_body("/api/v1/recall")
        self.assertEqual(body["session_id"], "hermes_s1")
        self.assertEqual(body["datasets"], ["hermes"])
        self.assertEqual(body["top_k"], 5)

    def test_none_targets_are_omitted(self):
        opener, _ = self._recall(datasets=None)
        self.assertNotIn("datasets", opener.json_body("/api/v1/recall"))

        opener, _ = self._recall(session_id=None)
        self.assertNotIn("session_id", opener.json_body("/api/v1/recall"))

    def test_query_type_is_sent_as_search_type_uppercased(self):
        opener, _ = self._recall(query_type="chunks")
        body = opener.json_body("/api/v1/recall")
        self.assertEqual(body["search_type"], "CHUNKS")
        self.assertNotIn("query_type", body)

    def test_absent_query_type_is_omitted(self):
        self.assertNotIn("search_type", self._recall()[0].json_body("/api/v1/recall"))

    def test_api_key_header_is_sent(self):
        opener, _ = self._recall()
        self.assertEqual(opener.request_for("/api/v1/recall")["headers"]["x-api-key"], "k")

    def test_results_are_returned_as_a_list(self):
        _, results = self._recall()
        self.assertEqual(results, [{"text": "hit"}])

    def test_a_single_object_response_is_wrapped(self):
        opener = FakeOpener({"/api/v1/recall": {"text": "one"}})
        results = _backend(opener).recall(
            query="q",
            session_id=None,
            datasets=None,
            top_k=1,
            auto_route=True,
            query_type=None,
            timeout=_TIMEOUT,
        )
        self.assertEqual(results, [{"text": "one"}])

    def test_auto_route_false_becomes_an_explicit_graph_completion(self):
        # There is no auto_route field, but the setting is expressible: server-side
        # auto_route=False with no type means "skip the classifier, use
        # GRAPH_COMPLETION", and naming that type bypasses the classifier too.
        opener, results = self._recall(auto_route=False)
        body = opener.json_body("/api/v1/recall")
        self.assertEqual(body["search_type"], "GRAPH_COMPLETION")
        self.assertNotIn("auto_route", body)
        self.assertEqual(results, [{"text": "hit"}])

    def test_auto_route_false_does_not_override_an_explicit_search_type(self):
        body = self._recall(auto_route=False, query_type="CHUNKS")[0].json_body("/api/v1/recall")
        self.assertEqual(body["search_type"], "CHUNKS")

    def test_auto_route_true_leaves_the_classifier_to_the_server(self):
        self.assertNotIn(
            "search_type", self._recall(auto_route=True)[0].json_body("/api/v1/recall")
        )


class TestRememberWireFormat(unittest.TestCase):
    def test_a_turn_is_a_typed_qa_entry_not_a_document_upload(self):
        # Live-diagnosed: /api/v1/remember takes its payload as a multipart file,
        # which the server coerces to "[UploadFile]" and then *skips* for the
        # session cache — reporting status "session_stored" while storing nothing.
        # Turns must go through the typed-entry endpoint.
        opener = FakeOpener({"/api/v1/remember/entry": {"status": "session_stored"}})
        result = _backend(opener).remember_session(
            text="User: hi\nAssistant: hello",
            session_id="hermes_s1",
            dataset="hermes",
            timeout=_TIMEOUT,
        )
        body = opener.json_body("/api/v1/remember/entry")
        self.assertEqual(body["dataset_name"], "hermes")
        self.assertEqual(body["session_id"], "hermes_s1")
        self.assertEqual(body["entry"]["type"], "qa")
        self.assertEqual(body["entry"]["answer"], "User: hi\nAssistant: hello")
        self.assertEqual(result.status, "session_stored")

    def test_a_turn_does_not_touch_the_document_endpoint(self):
        opener = FakeOpener({"/api/v1/remember/entry": {}})
        _backend(opener).remember_session(text="t", session_id="s", dataset="d", timeout=_TIMEOUT)
        self.assertEqual(opener.paths(), ["/api/v1/remember/entry"])

    def test_permanent_write_omits_the_session_id(self):
        # No session_id makes the server do a direct add + cognify.
        opener = FakeOpener({"/api/v1/remember": {"status": "completed"}})
        _backend(opener).remember_permanent(
            text="a fact", dataset="hermes", session_ids=[], timeout=_TIMEOUT
        )
        fields = opener.multipart_fields("/api/v1/remember")
        self.assertNotIn("session_id", fields)
        self.assertEqual(fields["data"], "a fact")

    def test_permanent_write_warns_that_session_ids_cannot_be_sent(self):
        opener = FakeOpener({"/api/v1/remember": {"status": "completed"}})
        with self.assertLogs("cognee_integration_hermes.http_backend", level="WARNING") as logs:
            _backend(opener).remember_permanent(
                text="a fact", dataset="hermes", session_ids=["hermes_s1"], timeout=_TIMEOUT
            )
        self.assertIn("session_ids", "\n".join(logs.output))
        self.assertNotIn("session_ids", opener.multipart_fields("/api/v1/remember"))

    def test_a_permanent_write_is_multipart(self):
        opener = FakeOpener({"/api/v1/remember": {}})
        _backend(opener).remember_permanent(text="t", dataset="d", session_ids=[], timeout=_TIMEOUT)
        content_type = opener.request_for("/api/v1/remember")["headers"]["content-type"]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))

    def test_result_exposes_status_as_an_attribute(self):
        # The provider reads getattr(result, "status", "completed"); a bare dict
        # would silently report "completed" for every write.
        opener = FakeOpener({"/api/v1/remember": {"status": "running"}})
        result = _backend(opener).remember_permanent(
            text="t", dataset="d", session_ids=[], timeout=_TIMEOUT
        )
        self.assertEqual(result.status, "running")

    def test_missing_status_defaults_to_completed(self):
        opener = FakeOpener({"/api/v1/remember": {}})
        result = _backend(opener).remember_permanent(
            text="t", dataset="d", session_ids=[], timeout=_TIMEOUT
        )
        self.assertEqual(result.status, "completed")


class TestImproveWireFormat(unittest.TestCase):
    def test_session_ids_reach_the_wire(self):
        """The regression this transport exists to fix.

        ``cognee.improve(session_ids=[...])`` through the SDK's CloudClient never
        sends them (improve.py:128 forwards only node_name/**kwargs), so the
        session-to-graph bridge silently became a dataset-wide improve.
        """
        opener = FakeOpener({"/api/v1/improve": {"status": "started"}})
        _backend(opener).improve(
            dataset="hermes", session_ids=["hermes_s1"], background=True, timeout=_TIMEOUT
        )
        body = opener.json_body("/api/v1/improve")
        self.assertEqual(body["session_ids"], ["hermes_s1"])
        self.assertEqual(body["dataset_name"], "hermes")
        self.assertIs(body["run_in_background"], True)

    def test_background_false_is_sent_explicitly(self):
        opener = FakeOpener({"/api/v1/improve": {}})
        _backend(opener).improve(
            dataset="hermes", session_ids=["s"], background=False, timeout=_TIMEOUT
        )
        self.assertIs(opener.json_body("/api/v1/improve")["run_in_background"], False)

    def test_empty_session_ids_are_omitted(self):
        opener = FakeOpener({"/api/v1/improve": {}})
        _backend(opener).improve(
            dataset="hermes", session_ids=[], background=True, timeout=_TIMEOUT
        )
        self.assertNotIn("session_ids", opener.json_body("/api/v1/improve"))


class TestForgetWireFormat(unittest.TestCase):
    def _forget(self, **overrides):
        params = {
            "dataset": "hermes",
            "everything": False,
            "memory_only": False,
            "timeout": _TIMEOUT,
        }
        params.update(overrides)
        opener = FakeOpener({"/api/v1/forget": {"deleted": True}})
        _backend(opener).forget(**params)
        return opener.json_body("/api/v1/forget")

    def test_dataset_scoped_delete(self):
        body = self._forget()
        self.assertEqual(body["dataset"], "hermes")
        self.assertIs(body["everything"], False)
        self.assertIs(body["memory_only"], False)

    def test_everything_drops_the_dataset(self):
        body = self._forget(everything=True)
        self.assertIs(body["everything"], True)
        self.assertNotIn("dataset", body)

    def test_memory_only_passes_through(self):
        self.assertIs(self._forget(memory_only=True)["memory_only"], True)


class TestConnect(unittest.TestCase):
    def _opener(self, **overrides):
        responses = {
            "/health": {"status": "ok"},
            "/api/v1/agents/register": {"id": "conn-1"},
        }
        responses.update(overrides)
        return FakeOpener(responses)

    def test_health_check_then_register(self):
        opener = self._opener()
        backend = HttpBackend(opener=opener)
        backend.connect(url=_URL, api_key="k", timeout=_TIMEOUT)
        self.assertEqual(opener.paths()[0], "/health")
        self.assertIn("/api/v1/agents/register", opener.paths())
        self.assertTrue(backend.registered)

    def test_unreachable_server_raises(self):
        # cognee.serve() only logged a warning here and handed back a client, so a
        # bad COGNEE_BASE_URL looked like success until the first real call.
        opener = self._opener(**{"/health": urllib.error.URLError("refused")})
        backend = HttpBackend(opener=opener)
        with self.assertRaises(CogneeUnreachable):
            backend.connect(url=_URL, api_key="k", timeout=_TIMEOUT)

    def test_unhealthy_server_raises(self):
        opener = self._opener(**{"/health": urllib.error.HTTPError(_URL, 503, "down", {}, None)})
        backend = HttpBackend(opener=opener)
        with self.assertRaises(CogneeHttpError):
            backend.connect(url=_URL, api_key="k", timeout=_TIMEOUT)

    def test_registration_failure_is_not_fatal(self):
        # Registration only drives the server's idle-shutdown watchdog.
        opener = self._opener(
            **{"/api/v1/agents/register": urllib.error.HTTPError(_URL, 404, "nope", {}, None)}
        )
        backend = HttpBackend(opener=opener)
        backend.connect(url=_URL, api_key="k", timeout=_TIMEOUT)
        self.assertFalse(backend.registered)

    def test_trailing_slash_is_normalized(self):
        opener = self._opener()
        backend = HttpBackend(opener=opener)
        backend.connect(url=_URL + "/", api_key="k", timeout=_TIMEOUT)
        self.assertEqual(backend.url, _URL)

    def test_close_unregisters_only_when_registered(self):
        opener = self._opener(**{"/api/v1/agents/unregister": {"active_agents": 0}})
        backend = HttpBackend(opener=opener)
        backend.connect(url=_URL, api_key="k", timeout=_TIMEOUT)
        backend.close(timeout=_TIMEOUT)
        self.assertIn("/api/v1/agents/unregister", opener.paths())
        self.assertFalse(backend.registered)

        fresh = FakeOpener()
        HttpBackend(opener=fresh).close(timeout=_TIMEOUT)
        self.assertEqual(fresh.paths(), [])


class TestApiKeyResolution(unittest.TestCase):
    def setUp(self):
        # An unset cache_dir means the real shared ~/.cognee-plugin — tests must
        # never read a developer's actual key or overwrite it with a fake one.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_dir = tmp.name

    def _mint_opener(self):
        return FakeOpener(
            {
                "/health": {"status": "ok"},
                "/api/v1/auth/login": {"access_token": "jwt-1"},
                "/api/v1/auth/api-keys": {"key": "minted-key"},
                "/api/v1/agents/register": {},
            }
        )

    def _connect(self, opener, *, api_key="", url=_URL, cache_dir=None):
        backend = HttpBackend(opener=opener, cache_dir=cache_dir or self.cache_dir)
        backend.connect(url=url, api_key=api_key, timeout=_TIMEOUT)
        return backend

    def test_the_default_cache_is_the_shared_plugin_state_dir(self):
        from cognee_integration_hermes.config import SHARED_PLUGIN_STATE_DIR

        self.assertEqual(HttpBackend()._cache_dir, SHARED_PLUGIN_STATE_DIR)

    def test_a_configured_key_is_used_as_is(self):
        opener = self._mint_opener()
        backend = self._connect(opener, api_key="given")
        self.assertEqual(backend.api_key, "given")
        self.assertNotIn("/api/v1/auth/login", opener.paths())

    def test_a_key_is_minted_when_none_is_configured(self):
        opener = self._mint_opener()
        backend = self._connect(opener)
        self.assertEqual(backend.api_key, "minted-key")
        login = opener.request_for("/api/v1/auth/login")
        self.assertEqual(login["headers"]["content-type"], "application/x-www-form-urlencoded")
        self.assertIn(b"username=", login["body"])

    def test_minting_reuses_an_existing_key_when_the_server_lists_one(self):
        opener = self._mint_opener()
        opener.responses["/api/v1/auth/api-keys"] = [{"key": "existing-key"}]
        backend = self._connect(opener)
        self.assertEqual(backend.api_key, "existing-key")

    def test_a_minted_key_is_cached_and_reused(self):
        first = self._mint_opener()
        self._connect(first)
        self.assertIn("/api/v1/auth/login", first.paths())

        second = self._mint_opener()
        backend = self._connect(second)
        self.assertEqual(backend.api_key, "minted-key")
        self.assertNotIn("/api/v1/auth/login", second.paths())

    def test_the_cache_file_is_the_shared_plugin_format(self):
        # The cache is shared with claude-code/codex/openclaw — same filename,
        # same fields — so whichever plugin mints first, the rest reuse.
        self._connect(self._mint_opener())
        data = json.loads((Path(self.cache_dir) / "api_key.json").read_text(encoding="utf-8"))
        self.assertEqual(data["api_key"], "minted-key")
        self.assertEqual(data["base_url"], _URL)
        self.assertIn("updated_at", data)

    def test_a_key_cached_by_another_plugin_is_reused(self):
        (Path(self.cache_dir) / "api_key.json").write_text(
            json.dumps({"base_url": _URL, "api_key": "claude-minted", "updated_at": "x"}),
            encoding="utf-8",
        )
        opener = self._mint_opener()
        backend = self._connect(opener)
        self.assertEqual(backend.api_key, "claude-minted")
        self.assertNotIn("/api/v1/auth/login", opener.paths())

    def test_a_cached_key_for_a_different_url_is_ignored(self):
        self._connect(self._mint_opener())
        other = self._mint_opener()
        self._connect(other, url="http://127.0.0.1:9999")
        self.assertIn("/api/v1/auth/login", other.paths())

    def test_failed_minting_proceeds_without_a_key(self):
        # A LOCAL server with authentication disabled needs no key at all.
        opener = self._mint_opener()
        opener.responses["/api/v1/auth/login"] = urllib.error.HTTPError(
            _URL, 404, "no auth", {}, None
        )
        backend = self._connect(opener)
        self.assertEqual(backend.api_key, "")

    def test_a_remote_target_without_a_key_fails_at_connect(self):
        # Cognee Cloud exposes no login route to mint from; continuing without a
        # key would smear one clear startup error into a 401 on every call.
        opener = self._mint_opener()
        with mock.patch.dict("os.environ", {"COGNEE_API_KEY": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "COGNEE_API_KEY"):
                self._connect(opener, url=_REMOTE_URL)
        # And it never tried the default-user login against a remote host.
        self.assertNotIn("/api/v1/auth/login", opener.paths())

    def test_a_remote_target_with_an_env_key_connects(self):
        opener = self._mint_opener()
        with mock.patch.dict("os.environ", {"COGNEE_API_KEY": "cloud-key"}, clear=False):
            backend = self._connect(opener, url=_REMOTE_URL)
        self.assertEqual(backend.api_key, "cloud-key")
        self.assertNotIn("/api/v1/auth/login", opener.paths())

    def test_a_remote_target_with_a_cached_key_connects(self):
        (Path(self.cache_dir) / "api_key.json").write_text(
            json.dumps({"base_url": _REMOTE_URL, "api_key": "cached-cloud"}),
            encoding="utf-8",
        )
        with mock.patch.dict("os.environ", {"COGNEE_API_KEY": ""}, clear=False):
            backend = self._connect(self._mint_opener(), url=_REMOTE_URL)
        self.assertEqual(backend.api_key, "cached-cloud")


class TestErrorMapping(unittest.TestCase):
    def test_http_error_carries_the_status(self):
        opener = FakeOpener({"/api/v1/recall": urllib.error.HTTPError(_URL, 401, "nope", {}, None)})
        with self.assertRaises(CogneeHttpError) as caught:
            _backend(opener).recall(
                query="q",
                session_id=None,
                datasets=None,
                top_k=1,
                auto_route=True,
                query_type=None,
                timeout=_TIMEOUT,
            )
        self.assertEqual(caught.exception.status, 401)

    def test_transport_failure_is_unreachable(self):
        opener = FakeOpener({"/api/v1/recall": urllib.error.URLError("refused")})
        with self.assertRaises(CogneeUnreachable):
            _backend(opener).recall(
                query="q",
                session_id=None,
                datasets=None,
                top_k=1,
                auto_route=True,
                query_type=None,
                timeout=_TIMEOUT,
            )

    def test_an_error_shaped_2xx_body_is_an_error(self):
        opener = FakeOpener({"/api/v1/recall": {"error": "prerequisites not met"}})
        with self.assertRaises(CogneeHttpError):
            _backend(opener).recall(
                query="q",
                session_id=None,
                datasets=None,
                top_k=1,
                auto_route=True,
                query_type=None,
                timeout=_TIMEOUT,
            )

    def test_malformed_json_is_a_server_error_not_unreachability(self):
        opener = FakeOpener({"/api/v1/forget": "{not json"})
        with self.assertRaises(CogneeHttpError):
            _backend(opener).forget(
                dataset="d", everything=False, memory_only=False, timeout=_TIMEOUT
            )


class TestEmptyRecallHint(unittest.TestCase):
    def test_there_is_no_hint_over_http(self):
        # The server owns the vector store; the probe is in-process only.
        self.assertIsNone(HttpBackend(opener=FakeOpener()).empty_recall_hint())


class TestProviderOverHttp(unittest.TestCase):
    """The full stack: real provider, real HttpBackend, fake socket.

    The provider tests pin behaviour against the protocol and the tests above pin
    the protocol against the wire. This class is the join — the first place the two
    layers run together, and the cheapest way to catch a mismatch that both halves
    consider correct in isolation.
    """

    def _provider(self, opener, **kwargs):
        return make_provider(backend=_backend(opener), **kwargs)

    def test_recall_produces_the_model_visible_envelope(self):
        opener = FakeOpener({"/api/v1/recall": [{"text": "alpha"}, {"text": "beta"}]})
        provider = self._provider(opener)
        out = json.loads(provider.handle_tool_call("cognee_recall", {"query": "q"}))
        self.assertEqual(out["count"], 2)
        self.assertEqual([item["text"] for item in out["results"]], ["alpha", "beta"])

    def test_recall_miss_produces_the_miss_envelope(self):
        opener = FakeOpener({"/api/v1/recall": []})
        out = json.loads(self._provider(opener).handle_tool_call("cognee_recall", {"query": "q"}))
        self.assertEqual(out, {"result": "No relevant Cognee memory found.", "count": 0})

    def test_scope_routing_survives_the_whole_stack(self):
        opener = FakeOpener({"/api/v1/recall": []})
        provider = self._provider(opener, session_cognee_id="hermes_sX")
        provider.handle_tool_call("cognee_recall", {"query": "q", "scope": "graph"})
        body = opener.json_body("/api/v1/recall")
        self.assertEqual(body["datasets"], ["hermes"])
        self.assertNotIn("session_id", body)

    def test_remember_reports_the_server_status(self):
        # The trap: an HTTP dict has no .status attribute, so a bare dict here
        # would make every write report "completed".
        opener = FakeOpener({"/api/v1/remember": {"status": "running"}})
        out = json.loads(
            self._provider(opener).handle_tool_call("cognee_remember", {"content": "fact"})
        )
        self.assertEqual(out, {"result": "Content stored in Cognee.", "status": "running"})

    def test_a_turn_goes_to_the_session_cache(self):
        opener = FakeOpener({"/api/v1/remember/entry": {"status": "session_stored"}})
        provider = self._provider(opener, session_cognee_id="hermes_sY")
        provider.sync_turn("hello", "hi there")
        provider._sync_thread.join(timeout=2.0)
        body = opener.json_body("/api/v1/remember/entry")
        self.assertEqual(body["session_id"], "hermes_sY")
        self.assertEqual(body["entry"]["answer"], "User: hello\nAssistant: hi there")

    def test_session_end_bridges_the_session_into_the_graph(self):
        """End-to-end proof that finding #2 is fixed."""
        opener = FakeOpener({"/api/v1/improve": {"status": "started"}})
        provider = self._provider(opener, session_cognee_id="hermes_sZ")
        provider.on_session_end([])
        body = opener.json_body("/api/v1/improve")
        self.assertEqual(body["session_ids"], ["hermes_sZ"])
        self.assertEqual(body["dataset_name"], "hermes")

    def test_a_backend_failure_becomes_the_error_envelope_and_trips_the_breaker(self):
        opener = FakeOpener({"/api/v1/recall": urllib.error.URLError("refused")})
        provider = self._provider(opener)
        for _ in range(5):
            out = json.loads(provider.handle_tool_call("cognee_recall", {"query": "q"}))
            self.assertTrue(out["error"].startswith("Cognee recall failed:"))
        self.assertTrue(provider._is_breaker_open())

    def test_forget_reaches_the_endpoint_and_reports_details(self):
        opener = FakeOpener({"/api/v1/forget": {"deleted": 2}})
        out = json.loads(
            self._provider(opener).handle_tool_call("cognee_forget", {"dataset": "hermes"})
        )
        self.assertEqual(out, {"result": "Cognee memory deleted.", "details": {"deleted": 2}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
