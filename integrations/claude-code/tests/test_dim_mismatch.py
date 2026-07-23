"""Unit tests for the embedding-dimension probe (_plugin_common.py).

When the embedding model changes between writing and reading, stored vectors and
fresh query vectors differ in size, so recall silently matches nothing.
``embedding_dimension_mismatch_hint`` returns a one-line actionable diagnostic
naming both dims and the active model on a confirmed mismatch, and None in every
other case (matching dims, no data, or any error) so recall keeps its normal
empty-result behavior.

The stored dim is read from the store's SCHEMA over a direct lancedb connection
(injectable ``connect``), never through the engine's connection: cognee's default
subprocess-proxy mode exposes no ``query().limit()``, so row sampling through the
engine breaks there — the fakes below deliberately mirror the schema surface so
that regression cannot be masked again.

Run: `pytest integrations/claude-code/tests/test_dim_mismatch.py`
(or `python integrations/claude-code/tests/test_dim_mismatch.py` standalone).
"""

import asyncio
import os
import pathlib
import sys
import tempfile
import time

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
    return asyncio.run(pc.embedding_dimension_mismatch_hint(engine, connect=_connect_for(tables)))


def test_mismatch_names_both_dims_and_model():
    msg = _hint(_FakeEngine(query_size=3072), {"Entity_name": _vector_table(1536)})
    assert msg is not None
    assert "1536" in msg and "3072" in msg
    assert "text-embedding-3-large" in msg and "openai" in msg


def test_matching_dims_returns_none():
    assert _hint(_FakeEngine(query_size=1536), {"Entity_name": _vector_table(1536)}) is None


def test_no_tables_returns_none():
    assert _hint(_FakeEngine(query_size=3072), {}) is None


def test_enumerates_nonstandard_collection():
    # Schema enumeration (not a fixed name list) means a custom-pipeline
    # collection is still sampled, so the diagnostic fires for those stores too.
    msg = _hint(_FakeEngine(query_size=3072), {"CustomThing_body": _vector_table(1536)})
    assert msg is not None and "1536" in msg and "3072" in msg


def test_table_without_vector_column_is_skipped():
    tables = {"weird": _FakeTable(_FakeSchema({"id": object()}))}
    assert _hint(_FakeEngine(query_size=3072), tables) is None


def test_non_fixed_size_vector_type_is_skipped():
    # A variable-size list type has no ``list_size`` -> indeterminate -> no hint.
    tables = {"Entity_name": _FakeTable(_FakeSchema({"vector": object()}))}
    assert _hint(_FakeEngine(query_size=3072), tables) is None


def test_unreadable_table_returns_none():
    # A read error must NOT surface a hint: a transient failure on an otherwise
    # healthy store would otherwise nag on a genuine empty recall (a false alarm).
    tables = {"Entity_name": _FakeTable(RuntimeError("store locked"))}
    assert _hint(_FakeEngine(query_size=3072), tables) is None


def test_cloud_store_is_never_probed():
    # api_key set = LanceDB Cloud; the store is not ours to introspect. The
    # direct connect must not even be attempted.
    async def _explode(_url):
        raise AssertionError("cloud store must not be probed")

    engine = _FakeEngine(query_size=3072, api_key="secret")
    msg = asyncio.run(pc.embedding_dimension_mismatch_hint(engine, connect=_explode))
    assert msg is None


def test_engine_without_url_returns_none():
    # A non-LanceDB backend (no ``url``) fails safe to the normal empty result.
    engine = _FakeEngine(query_size=3072, url="")
    assert _hint(engine, {"Entity_name": _vector_table(1536)}) is None


def test_broken_engine_returns_none():
    class _Broken:
        @property
        def embedding_engine(self):
            raise RuntimeError("boom")

    assert _hint(_Broken(), {"Entity_name": _vector_table(1536)}) is None


class _tmp_state:
    """Point the memo + prober marker at a temp dir for one test."""

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        self._saved = (pc._DIM_MEMO_FILE, pc._DIM_PROBE_MARKER)
        base = pathlib.Path(self._dir.name)
        pc._DIM_MEMO_FILE = base / "dim_check.json"
        pc._DIM_PROBE_MARKER = base / "dim_probe.started.json"
        return self

    def __exit__(self, *exc):
        pc._DIM_MEMO_FILE, pc._DIM_PROBE_MARKER = self._saved
        self._dir.cleanup()


def test_dim_memo_roundtrip():
    # A completed probe result is cached keyed by the embedder signature: a
    # matching signature reuses it (message, incl. a cached None = "no
    # mismatch"); a different signature is a miss so the probe re-runs.
    with _tmp_state():
        assert pc._read_dim_memo("sigA") is None  # cold: nothing cached
        pc._write_dim_memo("sigA", "MSG")
        assert pc._read_dim_memo("sigA")["message"] == "MSG"  # hit
        assert pc._read_dim_memo("sigB") is None  # signature changed -> re-probe
        pc._write_dim_memo("sigA", None)  # "checked, dims match"
        hit = pc._read_dim_memo("sigA")
        assert hit is not None and hit["message"] is None  # cached None is a hit


def test_bounded_hint_memo_hit_skips_prober():
    with _tmp_state():
        pc._write_dim_memo(pc._embedder_signature(), "CACHED-DIAG")
        saved = pc._spawn_dim_probe
        pc._spawn_dim_probe = lambda _sig: (_ for _ in ()).throw(AssertionError("spawned"))
        try:
            assert asyncio.run(pc.bounded_dim_mismatch_hint(timeout=0.2)) == "CACHED-DIAG"
        finally:
            pc._spawn_dim_probe = saved


def test_bounded_hint_miss_spawns_prober_and_picks_up_memo():
    # A memo miss kicks the detached prober; the poll loop then returns the
    # message the prober writes. Simulated with a prober that writes instantly.
    with _tmp_state():
        calls = []
        saved = pc._spawn_dim_probe

        def _fake_spawn(sig):
            calls.append(sig)
            pc._write_dim_memo(sig, "FRESH-DIAG")

        pc._spawn_dim_probe = _fake_spawn
        try:
            assert asyncio.run(pc.bounded_dim_mismatch_hint(timeout=1.0)) == "FRESH-DIAG"
            assert calls == [pc._embedder_signature()]
        finally:
            pc._spawn_dim_probe = saved


def test_bounded_hint_slow_prober_times_out_to_none():
    # A prober that hasn't finished by the deadline must not stall the hook: the
    # hint returns None on time and the result lands in the memo for next turn.
    with _tmp_state():
        saved = pc._spawn_dim_probe
        pc._spawn_dim_probe = lambda _sig: None  # prober never completes
        try:
            t0 = time.monotonic()
            assert asyncio.run(pc.bounded_dim_mismatch_hint(timeout=0.15)) is None
            assert time.monotonic() - t0 < 1.0
        finally:
            pc._spawn_dim_probe = saved


def test_detached_probe_writes_memo():
    # The prober entry point runs the full probe and memoizes the outcome —
    # including a diagnostic — under the current embedder signature.
    with _tmp_state():
        saved = pc.embedding_dimension_mismatch_hint

        async def _fake_hint(*_a, **_k):
            return "PROBED-DIAG"

        pc.embedding_dimension_mismatch_hint = _fake_hint
        try:
            pc._detached_dim_probe()
            memo = pc._read_dim_memo(pc._embedder_signature())
            assert memo is not None and memo["message"] == "PROBED-DIAG"
        finally:
            pc.embedding_dimension_mismatch_hint = saved


def test_spawn_probe_is_debounced():
    # Back-to-back misses must not stack prober processes: the second call
    # within the debounce window sees the fresh marker and returns early.
    with _tmp_state():
        import subprocess

        launches = []
        saved = subprocess.Popen
        subprocess.Popen = lambda *a, **k: launches.append(a) or None
        try:
            pc._spawn_dim_probe("sigX")
            pc._spawn_dim_probe("sigX")
            assert len(launches) == 1
        finally:
            subprocess.Popen = saved


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
