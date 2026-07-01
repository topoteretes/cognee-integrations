"""Unit tests for the embedding-dimension probe (_plugin_common.py).

When the embedding model changes between writing and reading, stored vectors and
fresh query vectors differ in size, so recall silently matches nothing.
``embedding_dimension_report`` classifies a zero-result recall as:
  - "mismatch"   -> a one-line actionable diagnostic naming both dims + model
  - "unverified" -> data present but dim unreadable -> a hedged, low-noise hint
  - "match"      -> dims agree (a genuine miss) -> no message
  - "no_data"    -> nothing to compare / fresh-empty session -> no message
Tests inject a fake vector engine so cognee is not required.

Run: `pytest integrations/claude-code/tests/test_dim_mismatch.py`
(or `python integrations/claude-code/tests/test_dim_mismatch.py` standalone).
"""

import asyncio
import os
import pathlib
import sys

# Pin the loop-guard so importing _plugin_common never re-execs the test process
# into the plugin venv (its module-level _reexec_into_venv()).
os.environ.setdefault("COGNEE_PLUGIN_IN_VENV", "1")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import _plugin_common as pc  # noqa: E402


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
    """Class name is not 'PGVectorAdapter', so the LanceDB row-sample path runs."""

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
    """A collection exists, but reading it raises -> 'unverified'."""

    def __init__(self, query_size):
        self.embedding_engine = _FakeEmbed(query_size)

    async def has_collection(self, name):
        return name == "Entity_name"

    async def get_collection(self, _name):
        raise RuntimeError("store locked")


class _FakeVecType:
    def __init__(self, dim):
        self.dim = dim


class _FakeVectorCol:
    def __init__(self, dim):
        self.type = _FakeVecType(dim)


class _FakeColumns:
    def __init__(self, dim):
        self.vector = _FakeVectorCol(dim)


class _FakeTable:
    def __init__(self, dim):
        self.c = _FakeColumns(dim)


class PGVectorAdapter:
    """Name matches the real adapter so the pgvector column-dim path runs."""

    def __init__(self, query_size, stored_dim):
        self.embedding_engine = _FakeEmbed(query_size)
        self._stored_dim = stored_dim

    async def has_collection(self, name):
        return name == "Entity_name"

    async def get_table(self, _name):
        return _FakeTable(self._stored_dim)


def _report(engine):
    return asyncio.run(pc.embedding_dimension_report(engine))


def _hint(engine):
    return asyncio.run(pc.embedding_dimension_mismatch_hint(engine))


def test_lancedb_mismatch_names_both_dims_and_model():
    status, msg = _report(_FakeLanceEngine(query_size=3072, stored_vector=[0.0] * 1536))
    assert status == "mismatch"
    assert "1536" in msg and "3072" in msg
    assert "text-embedding-3-large" in msg and "openai" in msg


def test_pgvector_mismatch_names_both_dims():
    status, msg = _report(PGVectorAdapter(query_size=768, stored_dim=1536))
    assert status == "mismatch"
    assert "1536" in msg and "768" in msg


def test_matching_dims_is_match_no_message():
    assert _report(_FakeLanceEngine(query_size=1536, stored_vector=[0.0] * 1536)) == ("match", None)


def test_no_collections_is_no_data():
    engine = _FakeLanceEngine(query_size=3072, stored_vector=[0.0] * 1536, present=())
    assert _report(engine) == ("no_data", None)


def test_empty_collection_is_no_data():
    # Collection present but holds no rows -> normal fresh/empty state, no hint.
    assert _report(_FakeLanceEngine(query_size=3072, stored_vector=None)) == ("no_data", None)


def test_unreadable_store_is_unverified_with_hedged_hint():
    status, msg = _report(_FakeErrorEngine(query_size=3072))
    assert status == "unverified"
    assert msg and "could not verify" in msg
    # The hedged hint must NOT read like the hard mismatch diagnostic.
    assert "changed" not in msg


def test_broken_engine_is_no_data():
    class _Broken:
        @property
        def embedding_engine(self):
            raise RuntimeError("boom")

    assert _report(_Broken()) == ("no_data", None)


def test_backcompat_hint_returns_message_only_on_mismatch():
    assert _hint(_FakeLanceEngine(query_size=3072, stored_vector=[0.0] * 1536)) is not None
    # unverified / match / no_data all yield None through the back-compat wrapper.
    assert _hint(_FakeErrorEngine(query_size=3072)) is None
    assert _hint(_FakeLanceEngine(query_size=1536, stored_vector=[0.0] * 1536)) is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
