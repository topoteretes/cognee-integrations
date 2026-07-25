"""Unit tests for the embedding-dimension probe (_plugin_common.py).

When the embedding model changes between writing and reading, stored vectors and
fresh query vectors differ in size, so recall silently matches nothing.
``embedding_dimension_mismatch_hint`` returns a one-line actionable diagnostic
naming both dims and the active model on a confirmed mismatch, and None in every
other case (matching dims, no data, or any error) so recall keeps its normal
empty-result behavior. Tests inject a fake vector engine so cognee is not required.

Run: `pytest integrations/claude-code/tests/test_dim_mismatch.py`
(or `python integrations/claude-code/tests/test_dim_mismatch.py` standalone).
"""

import asyncio
import os
import pathlib
import sys
import tempfile

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


class _FakeConnection:
    def __init__(self, names):
        self._names = list(names)

    async def table_names(self):
        return self._names


class _FakeLanceEngine:
    def __init__(self, query_size, stored_vector, present=("Entity_name",)):
        self.embedding_engine = _FakeEmbed(query_size)
        self._stored_vector = stored_vector
        self._present = tuple(present)

    async def get_connection(self):
        return _FakeConnection(self._present)

    async def get_collection(self, _name):
        rows = [{"vector": self._stored_vector}] if self._stored_vector is not None else []
        return _FakeCollection(rows)


class _FakeErrorEngine:
    """A collection exists, but reading it raises -> fail-safe None (no false alarm)."""

    def __init__(self, query_size):
        self.embedding_engine = _FakeEmbed(query_size)

    async def get_connection(self):
        return _FakeConnection(("Entity_name",))

    async def get_collection(self, _name):
        raise RuntimeError("store locked")


def _hint(engine):
    return asyncio.run(pc.embedding_dimension_mismatch_hint(engine))


def test_mismatch_names_both_dims_and_model():
    msg = _hint(_FakeLanceEngine(query_size=3072, stored_vector=[0.0] * 1536))
    assert msg is not None
    assert "1536" in msg and "3072" in msg
    assert "text-embedding-3-large" in msg and "openai" in msg


def test_matching_dims_returns_none():
    assert _hint(_FakeLanceEngine(query_size=1536, stored_vector=[0.0] * 1536)) is None


def test_enumerates_nonstandard_collection():
    # Enumeration (not a fixed name list) means a custom-pipeline collection is still
    # sampled, so the diagnostic fires for non-standard stores too.
    engine = _FakeLanceEngine(
        query_size=3072, stored_vector=[0.0] * 1536, present=("CustomThing_body",)
    )
    msg = _hint(engine)
    assert msg is not None and "1536" in msg and "3072" in msg


def test_no_collections_returns_none():
    engine = _FakeLanceEngine(query_size=3072, stored_vector=[0.0] * 1536, present=())
    assert _hint(engine) is None


def test_empty_collection_returns_none():
    # Collection present but holds no rows -> normal fresh/empty state, no hint.
    assert _hint(_FakeLanceEngine(query_size=3072, stored_vector=None)) is None


def test_unreadable_store_returns_none():
    # A read error must NOT surface a hint: a transient lock on an otherwise-healthy
    # store would otherwise nag on a genuine empty recall (a false alarm).
    assert _hint(_FakeErrorEngine(query_size=3072)) is None


def test_broken_engine_returns_none():
    class _Broken:
        @property
        def embedding_engine(self):
            raise RuntimeError("boom")

    assert _hint(_Broken()) is None


def test_dim_memo_roundtrip():
    # The per-prompt wrapper caches a completed probe result keyed by the embedder
    # signature: a matching signature reuses it (message, incl. a cached None = "no
    # mismatch"); a different signature is a miss so the probe re-runs.
    with tempfile.TemporaryDirectory() as tmp:
        saved = pc._DIM_MEMO_FILE
        pc._DIM_MEMO_FILE = pathlib.Path(tmp) / "dim_check.json"
        try:
            assert pc._read_dim_memo("sigA") is None  # cold: nothing cached
            pc._write_dim_memo("sigA", "MSG")
            assert pc._read_dim_memo("sigA")["message"] == "MSG"  # hit
            assert pc._read_dim_memo("sigB") is None  # signature changed -> re-probe
            pc._write_dim_memo("sigA", None)  # "checked, dims match"
            hit = pc._read_dim_memo("sigA")
            assert hit is not None and hit["message"] is None  # cached None is a hit
        finally:
            pc._DIM_MEMO_FILE = saved


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
