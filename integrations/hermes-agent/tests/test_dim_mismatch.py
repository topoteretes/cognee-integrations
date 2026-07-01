"""Tests for the embedding-dimension probe in the Hermes provider.

``_dimension_report`` classifies a zero-result recall as "mismatch" (actionable
diagnostic), "unverified" (data present but dim unreadable -> hedged hint),
"match" (genuine miss), or "no_data" (nothing to compare / cannot introspect).
A fake vector engine is injected so cognee is not required.

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


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def limit(self, _n):
        return self

    async def to_list(self):
        return self._rows


class _FakeCollection:
    def __init__(self, rows):
        self._rows = rows

    def query(self):
        return _FakeQuery(self._rows)


class _FakeLanceEngine:
    def __init__(self, query_size, stored_vector, present=("Entity_name",)):
        self.embedding_engine = _FakeEmbed(query_size)
        self._stored_vector = stored_vector
        self._present = set(present)

    async def has_collection(self, name):
        return name in self._present

    async def get_collection(self, _name):
        rows = [{"vector": self._stored_vector}] if self._stored_vector is not None else []
        return _FakeCollection(rows)


class _FakeErrorEngine:
    def __init__(self, query_size):
        self.embedding_engine = _FakeEmbed(query_size)

    async def has_collection(self, name):
        return name == "Entity_name"

    async def get_collection(self, _name):
        raise RuntimeError("store locked")


def _report(engine):
    return asyncio.run(provider_mod._dimension_report(engine))


class TestDimensionReport(unittest.TestCase):
    def test_mismatch_names_both_dims_and_model(self):
        status, msg = _report(_FakeLanceEngine(query_size=3072, stored_vector=[0.0] * 1536))
        self.assertEqual(status, "mismatch")
        self.assertIn("1536", msg)
        self.assertIn("3072", msg)
        self.assertIn("text-embedding-3-large", msg)

    def test_matching_dims_is_match(self):
        self.assertEqual(
            _report(_FakeLanceEngine(query_size=1536, stored_vector=[0.0] * 1536)), ("match", None)
        )

    def test_no_collections_is_no_data(self):
        engine = _FakeLanceEngine(query_size=3072, stored_vector=[0.0] * 1536, present=())
        self.assertEqual(_report(engine), ("no_data", None))

    def test_unreadable_store_is_unverified(self):
        status, msg = _report(_FakeErrorEngine(query_size=3072))
        self.assertEqual(status, "unverified")
        self.assertIn("could not verify", msg)

    def test_broken_engine_is_no_data(self):
        class _Broken:
            @property
            def embedding_engine(self):
                raise RuntimeError("boom")

        self.assertEqual(_report(_Broken()), ("no_data", None))


class TestProviderGate(unittest.TestCase):
    def test_remote_mode_skips_check(self):
        provider = provider_mod.CogneeMemoryProvider()
        provider._remote_mode = True
        self.assertEqual(provider._embedding_dimension_report(), ("no_data", None))

    def test_embedded_mode_runs_check_via_bridge(self):
        provider = provider_mod.CogneeMemoryProvider()
        provider._remote_mode = False

        async def _fake_report():
            return ("mismatch", "DIM MISMATCH")

        original = provider_mod._dimension_report
        provider_mod._dimension_report = _fake_report
        try:
            self.assertEqual(
                provider._embedding_dimension_report(), ("mismatch", "DIM MISMATCH")
            )
        finally:
            provider_mod._dimension_report = original
            provider._bridge.shutdown()


if __name__ == "__main__":
    unittest.main()
