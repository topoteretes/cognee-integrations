"""Unit tests for the embedding-dimension mismatch hint (_plugin_common.py).

When the embedding model changes between writing and reading, stored vectors and
fresh query vectors differ in size, so recall silently matches nothing. The hint
turns that silent miss into a one-line actionable error naming both dimensions
and the active embedder. These tests inject a fake vector engine so cognee is
not required.

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


def _hint(engine):
    return asyncio.run(pc.embedding_dimension_mismatch_hint(engine))


def test_lancedb_mismatch_names_both_dims_and_model():
    msg = _hint(_FakeLanceEngine(query_size=3072, stored_vector=[0.0] * 1536))
    assert msg is not None
    assert "1536" in msg  # stored dim
    assert "3072" in msg  # query dim
    assert "text-embedding-3-large" in msg  # active model
    assert "openai" in msg  # provider


def test_pgvector_mismatch_names_both_dims():
    msg = _hint(PGVectorAdapter(query_size=768, stored_dim=1536))
    assert msg is not None
    assert "1536" in msg
    assert "768" in msg


def test_matching_dims_returns_none():
    assert _hint(_FakeLanceEngine(query_size=1536, stored_vector=[0.0] * 1536)) is None


def test_no_collections_returns_none():
    assert _hint(_FakeLanceEngine(query_size=3072, stored_vector=[0.0] * 1536, present=())) is None


def test_empty_collection_returns_none():
    # Collection present but holds no rows -> stored dim indeterminate -> no false error.
    assert _hint(_FakeLanceEngine(query_size=3072, stored_vector=None)) is None


def test_never_raises_on_broken_engine():
    class _Broken:
        @property
        def embedding_engine(self):
            raise RuntimeError("boom")

    assert _hint(_Broken()) is None


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
