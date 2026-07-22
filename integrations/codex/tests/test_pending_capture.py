"""Codex mirror of the cognify-lag capture tests (topoteretes/cognee#3553).

The feature is duplicated across the claude-code and codex plugin trees; the
claude-code copy is covered by integrations/claude-code/tests/. This is the
codex mirror — it pins the behaviour that is codex-specific or that a silent
copy-drift between the two _plugin_common.py / _cognee_client.py files would
break:

- pending_capture_counts detects captured-but-uncognified content and normalizes
  the session id to the sanitized write-side key (so the explicit-search path's
  raw ${COGNEE_SESSION_ID} fallback still matches);
- the colon-anchored suffix match cannot split a session id;
- annotate_empty_recall emits the codex hint (python3 sync-session-to-graph.py)
  and no unconsumed `authoritative` field, and passes non-empty results through.

Run: python integrations/codex/tests/test_pending_capture.py (or via pytest).
"""

import hashlib
import json
import pathlib
import sys
import tempfile

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts")
)

import _cognee_client as cc  # noqa: E402
import _plugin_common as pc  # noqa: E402
from _recall_http import UNREACHABLE  # noqa: E402

SESSION = "codex_abc123"
DATASET = "agent_sessions"
KEY = f"{DATASET}:{SESSION}"


def _with_fresh_bridge_dir(fn):
    """Point pc._BRIDGE_DIR at a fresh temp dir for the test, then restore it."""

    def wrapper():
        orig = pc._BRIDGE_DIR
        pc._BRIDGE_DIR = pathlib.Path(tempfile.mkdtemp(prefix="cognee-bridge-codex-test-"))
        try:
            fn(pc._BRIDGE_DIR)
        finally:
            pc._BRIDGE_DIR = orig

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _write_bridge(tmp: pathlib.Path, cache, name: str = "scope.json") -> None:
    (tmp / name).write_text(json.dumps(cache), encoding="utf-8")


def _digest(session_cache: dict, kind: str) -> str:
    qa_doc, trace_doc = pc._format_bridge_docs(session_cache, SESSION)
    document = qa_doc if kind == "qa" else trace_doc
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _patched_pending(value):
    """Temporarily replace _plugin_common.pending_capture_counts; return a restorer."""
    orig = pc.pending_capture_counts
    pc.pending_capture_counts = value if callable(value) else (lambda session_id: value)

    def _restore():
        pc.pending_capture_counts = orig

    return _restore


@_with_fresh_bridge_dir
def test_uncognified_entries_are_pending(tmp):
    _write_bridge(
        tmp,
        {KEY: {"qa": [{"question": "q1", "answer": "a1"}], "trace": ["tool call one"]}},
    )
    assert pc.pending_capture_counts(SESSION) == {"qa": 1, "trace": 1}


@_with_fresh_bridge_dir
def test_fully_cognified_is_zero(tmp):
    session_cache = {"qa": [{"question": "q1", "answer": "a1"}], "trace": ["tool call one"]}
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
def test_unsanitized_session_id_matches_sanitized_key(tmp):
    # The explicit-search path may hand the reader a raw id; the write side keyed
    # the bridge on the sanitized one. The reader must normalize so they match.
    raw = "my project"
    sanitized = pc._sanitize_session_key(raw)  # "my_project"
    assert sanitized != raw
    _write_bridge(
        tmp,
        {f"{DATASET}:{sanitized}": {"qa": [{"question": "q1", "answer": "a1"}], "trace": []}},
    )
    assert pc.pending_capture_counts(raw) == {"qa": 1, "trace": 0}


@_with_fresh_bridge_dir
def test_suffix_match_is_colon_anchored(tmp):
    _write_bridge(
        tmp,
        {f"{DATASET}:xcodex_abc123": {"qa": [{"question": "q", "answer": "a"}], "trace": []}},
    )
    assert pc.pending_capture_counts(SESSION) == {"qa": 0, "trace": 0}


@_with_fresh_bridge_dir
def test_corrupted_bridge_file_is_zero_and_does_not_raise(tmp):
    (tmp / "scope.json").write_text("not json{", encoding="utf-8")
    assert pc.pending_capture_counts(SESSION) == {"qa": 0, "trace": 0}


def test_annotate_empty_recall_emits_codex_hint_without_authoritative():
    restore = _patched_pending({"qa": 2, "trace": 1})
    try:
        out = cc.annotate_empty_recall([], SESSION)
    finally:
        restore()
    assert out["recall"] == []
    assert out["captured_pending"] == {"qa": 2, "trace": 1}
    assert "authoritative" not in out  # dropped: nothing consumes it
    assert "error" not in out
    # Codex has no /cognee-memory:cognee-sync skill; it points at the script.
    assert "sync-session-to-graph.py" in out["hint"]


def test_annotate_empty_recall_without_pending_stays_empty():
    restore = _patched_pending({"qa": 0, "trace": 0})
    try:
        assert cc.annotate_empty_recall([], SESSION) == []
    finally:
        restore()


def test_annotate_passes_through_non_empty_results():
    def _boom(session_id):
        raise AssertionError("pending check must not run for non-empty results")

    restore = _patched_pending(_boom)
    try:
        hits = [{"text": "hit"}]
        err = {"error": "boom", "status": 500, "authoritative": False}
        assert cc.annotate_empty_recall(hits, SESSION) == hits
        assert cc.annotate_empty_recall(err, SESSION) == err
        assert cc.annotate_empty_recall(UNREACHABLE, SESSION) == UNREACHABLE
    finally:
        restore()


def test_annotate_never_raises_when_pending_check_fails():
    def _boom(session_id):
        raise OSError("disk on fire")

    restore = _patched_pending(_boom)
    try:
        assert cc.annotate_empty_recall([], SESSION) == []
    finally:
        restore()


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
