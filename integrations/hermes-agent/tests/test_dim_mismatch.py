"""Tests for the embedding-dimension probe in the Hermes provider.

``_dimension_mismatch_hint`` returns a one-line actionable diagnostic naming both
dims and the active model on a confirmed mismatch, and None in every other case
(matching dims, no data, or any error) so recall keeps its normal empty-result
behavior.

The stored dim is read from the store's SCHEMA over a direct lancedb connection
(injectable ``connect``), never through the engine's connection: cognee's default
subprocess-proxy mode exposes no ``query().limit()``, so row sampling through the
engine breaks there — the fakes below deliberately mirror the schema surface so
that regression cannot be masked again.

Runs under pytest or standalone (``python3 tests/test_dim_mismatch.py``).
"""

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cognee_integration_hermes import provider as provider_mod  # noqa: E402


class _FakeEmbed:
    def __init__(self, size, model="openai/text-embedding-3-large", provider="openai"):
        self._size = size
        self.model = model
        self.provider = provider

    def get_vector_size(self):
        return self._size


class _FixedSizeListType:
    """Duck-type of pyarrow's FixedSizeListType: carries only ``list_size``."""

    def __init__(self, list_size):
        self.list_size = list_size


class _FakeField:
    def __init__(self, type_):
        self.type = type_


class _FakeSchema:
    def __init__(self, fields):
        self._fields = fields  # name -> arrow-ish type object

    def field(self, name):
        if name not in self._fields:
            raise KeyError(name)
        return _FakeField(self._fields[name])


class _FakeTable:
    def __init__(self, schema):
        self._schema = schema

    async def schema(self):
        if isinstance(self._schema, Exception):
            raise self._schema
        return self._schema


class _FakeConnection:
    def __init__(self, tables):
        self._tables = tables  # name -> _FakeTable

    async def table_names(self):
        return list(self._tables)

    async def open_table(self, name):
        return self._tables[name]


def _connect_for(tables):
    async def _connect(_url):
        return _FakeConnection(tables)

    return _connect


def _vector_table(dim):
    return _FakeTable(_FakeSchema({"vector": _FixedSizeListType(dim), "id": object()}))


class _FakeEngine:
    def __init__(self, query_size, url="/tmp/fake-store.lancedb", api_key=None):
        self.embedding_engine = _FakeEmbed(query_size)
        self.url = url
        self.api_key = api_key


def _hint(engine, tables):
    return asyncio.run(provider_mod._dimension_mismatch_hint(engine, connect=_connect_for(tables)))


class TestDimensionMismatchHint(unittest.TestCase):
    def test_mismatch_names_both_dims_and_model(self):
        msg = _hint(_FakeEngine(query_size=3072), {"Entity_name": _vector_table(1536)})
        self.assertIsNotNone(msg)
        self.assertIn("1536", msg)
        self.assertIn("3072", msg)
        self.assertIn("text-embedding-3-large", msg)

    def test_matching_dims_returns_none(self):
        self.assertIsNone(_hint(_FakeEngine(query_size=1536), {"Entity_name": _vector_table(1536)}))

    def test_enumerates_nonstandard_collection(self):
        # Schema enumeration (not a fixed name list) means a custom-pipeline
        # collection is still sampled, so the diagnostic fires for those stores too.
        msg = _hint(_FakeEngine(query_size=3072), {"CustomThing_body": _vector_table(1536)})
        self.assertIsNotNone(msg)

    def test_no_tables_returns_none(self):
        self.assertIsNone(_hint(_FakeEngine(query_size=3072), {}))

    def test_table_without_vector_column_is_skipped(self):
        tables = {"weird": _FakeTable(_FakeSchema({"id": object()}))}
        self.assertIsNone(_hint(_FakeEngine(query_size=3072), tables))

    def test_unreadable_table_returns_none(self):
        # A read error must not surface a hint (no false alarm on a healthy store).
        tables = {"Entity_name": _FakeTable(RuntimeError("store locked"))}
        self.assertIsNone(_hint(_FakeEngine(query_size=3072), tables))

    def test_cloud_store_is_never_probed(self):
        # api_key set = LanceDB Cloud; the direct connect must not be attempted.
        async def _explode(_url):
            raise AssertionError("cloud store must not be probed")

        engine = _FakeEngine(query_size=3072, api_key="secret")
        msg = asyncio.run(provider_mod._dimension_mismatch_hint(engine, connect=_explode))
        self.assertIsNone(msg)

    def test_engine_without_url_returns_none(self):
        # A non-LanceDB backend (no ``url``) fails safe to the normal empty result.
        engine = _FakeEngine(query_size=3072, url="")
        self.assertIsNone(_hint(engine, {"Entity_name": _vector_table(1536)}))

    def test_broken_engine_returns_none(self):
        class _Broken:
            @property
            def embedding_engine(self):
                raise RuntimeError("boom")

        self.assertIsNone(_hint(_Broken(), {"Entity_name": _vector_table(1536)}))


class TestProviderGate(unittest.TestCase):
    def test_remote_mode_skips_check(self):
        provider = provider_mod.CogneeMemoryProvider()
        provider._remote_mode = True
        self.assertIsNone(provider._embedding_dimension_mismatch_hint())

    def test_embedded_mode_runs_check_via_bridge(self):
        provider = provider_mod.CogneeMemoryProvider()
        provider._remote_mode = False

        async def _fake_hint():
            return "DIM MISMATCH"

        original = provider_mod._dimension_mismatch_hint
        provider_mod._dimension_mismatch_hint = _fake_hint
        try:
            self.assertEqual(provider._embedding_dimension_mismatch_hint(), "DIM MISMATCH")
        finally:
            provider_mod._dimension_mismatch_hint = original
            provider._bridge.shutdown()


if __name__ == "__main__":
    unittest.main()
