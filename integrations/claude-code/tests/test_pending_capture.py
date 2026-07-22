"""Unit tests for pending_capture_counts (_plugin_common.py).

Covers the contract from topoteretes/cognee#3553:
- bridge qa/trace content whose digest is absent/stale in ``_state`` counts as
  captured-but-not-yet-cognified ("stored, not yet queryable");
- fully-drained content (digests match) counts as zero pending, so a genuine
  empty recall stays a genuine empty;
- other sessions' keys, corrupted files, and a missing bridge dir never leak
  counts or raise;
- the digest scheme stays pinned to the REAL drain
  (persist_session_cache_to_graph_via_http), not a parallel reimplementation.

Run: `pytest integrations/claude-code/tests/test_pending_capture.py`
(or `python integrations/claude-code/tests/test_pending_capture.py` standalone).
"""

import hashlib
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402

SESSION = "claude_abc123"
DATASET = "agent_sessions"
KEY = f"{DATASET}:{SESSION}"


def _with_fresh_bridge_dir(fn):
    """Point pc._BRIDGE_DIR at a fresh temp dir for the test, then restore it.

    Deliberately not functools.wraps: pytest would follow ``__wrapped__`` to the
    inner ``(tmp)`` signature and go looking for a ``tmp`` fixture.
    """

    def wrapper():
        orig = pc._BRIDGE_DIR
        pc._BRIDGE_DIR = pathlib.Path(tempfile.mkdtemp(prefix="cognee-bridge-test-"))
        try:
            fn(pc._BRIDGE_DIR)
        finally:
            pc._BRIDGE_DIR = orig

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _write_bridge(tmp: pathlib.Path, cache, name: str = "scope.json") -> None:
    payload = cache if isinstance(cache, str) else json.dumps(cache)
    (tmp / name).write_text(payload, encoding="utf-8")


def _digest(session_cache: dict, kind: str) -> str:
    """Compute the digest the drain would store for one kind of this cache."""
    qa_doc, trace_doc = pc._format_bridge_docs(session_cache, SESSION)
    document = qa_doc if kind == "qa" else trace_doc
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


@_with_fresh_bridge_dir
def test_missing_bridge_dir_is_zero(tmp):
    pc._BRIDGE_DIR = tmp / "nope"
    assert pc.pending_capture_counts(SESSION) == {"qa": 0, "trace": 0}


@_with_fresh_bridge_dir
def test_empty_session_id_is_zero(tmp):
    _write_bridge(tmp, {KEY: {"qa": [{"question": "q", "answer": "a"}], "trace": []}})
    assert pc.pending_capture_counts("") == {"qa": 0, "trace": 0}


@_with_fresh_bridge_dir
def test_uncognified_entries_are_pending(tmp):
    # Captured content with no _state at all: everything is pending.
    _write_bridge(
        tmp,
        {
            KEY: {
                "qa": [
                    {"question": "q1", "answer": "a1"},
                    {"question": "q2", "answer": "a2"},
                ],
                "trace": ["tool call one"],
            }
        },
    )
    assert pc.pending_capture_counts(SESSION) == {"qa": 2, "trace": 1}


@_with_fresh_bridge_dir
def test_fully_cognified_is_zero(tmp):
    # Digests match what the drain stored: a genuine empty stays a genuine empty.
    session_cache = {
        "qa": [{"question": "q1", "answer": "a1"}],
        "trace": ["tool call one"],
    }
    _write_bridge(
        tmp,
        {
            KEY: session_cache,
            "_state": {
                f"{KEY}:qa": _digest(session_cache, "qa"),
                f"{KEY}:trace": _digest(session_cache, "trace"),
            },
        },
    )
    assert pc.pending_capture_counts(SESSION) == {"qa": 0, "trace": 0}


@_with_fresh_bridge_dir
def test_partially_cognified_counts_only_unmarked(tmp):
    # qa drained, trace not: only trace is pending.
    session_cache = {
        "qa": [{"question": "q1", "answer": "a1"}],
        "trace": ["tool call one", "tool call two"],
    }
    _write_bridge(
        tmp,
        {
            KEY: session_cache,
            "_state": {f"{KEY}:qa": _digest(session_cache, "qa")},
        },
    )
    assert pc.pending_capture_counts(SESSION) == {"qa": 0, "trace": 2}


@_with_fresh_bridge_dir
def test_stale_digest_counts_as_pending(tmp):
    # Content appended AFTER the last drain: the stored digest no longer matches.
    drained = {"qa": [{"question": "q1", "answer": "a1"}], "trace": []}
    grown = {
        "qa": [
            {"question": "q1", "answer": "a1"},
            {"question": "q2", "answer": "a2"},
        ],
        "trace": [],
    }
    _write_bridge(
        tmp,
        {
            KEY: grown,
            "_state": {f"{KEY}:qa": _digest(drained, "qa")},
        },
    )
    assert pc.pending_capture_counts(SESSION) == {"qa": 2, "trace": 0}


@_with_fresh_bridge_dir
def test_other_session_keys_are_ignored(tmp):
    _write_bridge(
        tmp,
        {f"{DATASET}:claude_other999": {"qa": [{"question": "q", "answer": "a"}], "trace": []}},
    )
    assert pc.pending_capture_counts(SESSION) == {"qa": 0, "trace": 0}


@_with_fresh_bridge_dir
def test_corrupted_bridge_file_is_zero_and_does_not_raise(tmp):
    _write_bridge(tmp, "not json{")
    assert pc.pending_capture_counts(SESSION) == {"qa": 0, "trace": 0}


@_with_fresh_bridge_dir
def test_corrupt_shape_is_skipped_and_does_not_raise(tmp):
    # Valid JSON, corrupt SHAPE (qa entries are not dicts): the bad file is
    # skipped, a good file alongside it is still counted, nothing raises.
    _write_bridge(tmp, {KEY: {"qa": ["not-a-dict"], "trace": []}}, name="bad.json")
    _write_bridge(
        tmp,
        {KEY: {"qa": [{"question": "q1", "answer": "a1"}], "trace": []}},
        name="good.json",
    )
    assert pc.pending_capture_counts(SESSION) == {"qa": 1, "trace": 0}


@_with_fresh_bridge_dir
def test_pending_found_across_multiple_bridge_files(tmp):
    # The hooks and the search shell can resolve different bridge filenames
    # (COGNEE_SESSION_KEY vs fallback scope); the dir scan must find the
    # session's keys regardless of which file holds them.
    _write_bridge(tmp, {"other:key": {"qa": [], "trace": ["x"]}}, name="a.json")
    _write_bridge(
        tmp,
        {KEY: {"qa": [{"question": "q1", "answer": "a1"}], "trace": []}},
        name="b.json",
    )
    assert pc.pending_capture_counts(SESSION) == {"qa": 1, "trace": 0}


@_with_fresh_bridge_dir
def test_real_drain_clears_pending(tmp):
    # Pin pending_capture_counts to the REAL drain's _state key scheme: run
    # persist_session_cache_to_graph_via_http against a real bridge file (only
    # the HTTP seams mocked), then confirm pending drops to zero. If the drain's
    # state_key composition ever changed, this test fails instead of both sides
    # drifting apart silently.
    bridge_path = tmp / f"{SESSION}.json"
    bridge_path.write_text(
        json.dumps({KEY: {"qa": [{"question": "q1", "answer": "a1"}], "trace": ["tool call one"]}}),
        encoding="utf-8",
    )
    assert pc.pending_capture_counts(SESSION) == {"qa": 1, "trace": 1}

    saved = {
        k: getattr(pc, k)
        for k in (
            "_local_api_url",
            "_backend_reachable",
            "_api_key",
            "_bridge_file",
            "_post_remember_document",
            "wait_for_cognify",
            "hook_log",
        )
    }
    pc._local_api_url = lambda: "http://x"
    pc._backend_reachable = lambda url: True
    pc._api_key = lambda: "k"
    pc._bridge_file = lambda sid: bridge_path
    pc._post_remember_document = lambda *a, **k: {
        "ok": True,
        "dataset_id": "d1",
        "pipeline_run_id": "p1",
    }
    pc.wait_for_cognify = lambda *a, **k: "completed"
    pc.hook_log = lambda *a, **k: None
    try:
        wrote = pc.persist_session_cache_to_graph_via_http(DATASET, SESSION)
    finally:
        for k, v in saved.items():
            setattr(pc, k, v)

    assert wrote is True
    assert pc.pending_capture_counts(SESSION) == {"qa": 0, "trace": 0}


@_with_fresh_bridge_dir
def test_unsanitized_session_id_matches_sanitized_key(tmp):
    # The write path always keys the bridge on the SANITIZED id ("my project" ->
    # "my_project"); the explicit-search path may hand the reader the raw value
    # (cognee-search.sh's ${COGNEE_SESSION_ID} fallback). The reader must normalize
    # so the ":{session_id}" suffix still matches the sanitized bridge key.
    raw = "my project"
    sanitized = pc._sanitize_session_key(raw)  # "my_project"
    assert sanitized != raw
    _write_bridge(
        tmp,
        {f"{DATASET}:{sanitized}": {"qa": [{"question": "q1", "answer": "a1"}], "trace": []}},
    )
    assert pc.pending_capture_counts(raw) == {"qa": 1, "trace": 0}
    assert pc.pending_capture_counts(sanitized) == {"qa": 1, "trace": 0}


@_with_fresh_bridge_dir
def test_suffix_match_is_colon_anchored(tmp):
    # The docstring promises the ":{session_id}" match "cannot split a session id".
    # A key whose id is a non-colon-anchored tail of the session id must NOT match:
    # session "claude_abc123" must not be counted for key "...:xclaude_abc123".
    _write_bridge(
        tmp,
        {f"{DATASET}:xclaude_abc123": {"qa": [{"question": "q", "answer": "a"}], "trace": []}},
    )
    assert pc.pending_capture_counts(SESSION) == {"qa": 0, "trace": 0}


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as e:
                failures += 1
                print("FAIL", name, e)
    sys.exit(1 if failures else 0)
