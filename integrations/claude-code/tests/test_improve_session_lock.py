"""One improve per session, machine-wide — and recall always names its dataset.

Two regressions this locks down, both root-caused from a real hook.log:

1. **Duplicate improves.** The idle watcher, ``store-to-session`` and the
   SessionEnd sync all bridge sessions, and the outer ``sync_lock`` is bypassed in
   API mode (``nullcontext(True) if api_mode``). 67% of sessions were submitted by
   two processes at once; the server's own per-session lock answered the loser with
   ``{}`` (busy), driving a 15s retry loop for up to ten minutes, while concurrent
   writers collided on the single-writer graph store ("Could not set lock on file")
   and left pipeline runs stuck with the graph unwritten.

2. **Recall without a dataset.** ``/api/v1/recall`` was the only data-plane call
   omitting one, leaving the graph scope dependent on the session's dataset binding
   — an unbound session with several readable datasets is rejected as ambiguous
   rather than searched.

Run: `python integrations/claude-code/tests/test_improve_session_lock.py` (or via pytest).
"""

import importlib.util
import json
import pathlib
import sys
import tempfile

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _fresh_common(tmp: pathlib.Path):
    """Import _plugin_common with its lock dir pointed at a temp path."""
    sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        f"common_lock_{tmp.name}", _SCRIPTS / "_plugin_common.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._IMPROVE_LOCK_DIR = tmp / "improve-locks"
    mod.hook_log = lambda *a, **kw: None
    return mod


# --- the per-session improve claim ------------------------------------------


def test_second_holder_is_refused_for_the_same_session():
    tmp = pathlib.Path(tempfile.mkdtemp())
    c = _fresh_common(tmp)
    with c.improve_session_lock("sess-A", "first") as a:
        assert a is True, "first claimer must win"
        with c.improve_session_lock("sess-A", "second") as b:
            assert b is False, "concurrent claim on the SAME session must be refused"


def test_different_sessions_do_not_block_each_other():
    tmp = pathlib.Path(tempfile.mkdtemp())
    c = _fresh_common(tmp)
    with c.improve_session_lock("sess-A", "first") as a:
        with c.improve_session_lock("sess-B", "second") as b:
            assert a is True and b is True, "distinct sessions must run concurrently"


def test_lock_is_released_after_the_block():
    tmp = pathlib.Path(tempfile.mkdtemp())
    c = _fresh_common(tmp)
    with c.improve_session_lock("sess-A", "first") as a:
        assert a is True
    with c.improve_session_lock("sess-A", "second") as b:
        assert b is True, "lock must not leak once the holder finishes"


def test_lock_released_even_when_body_raises():
    tmp = pathlib.Path(tempfile.mkdtemp())
    c = _fresh_common(tmp)
    try:
        with c.improve_session_lock("sess-A", "boom"):
            raise RuntimeError("improve blew up")
    except RuntimeError:
        pass
    with c.improve_session_lock("sess-A", "after") as b:
        assert b is True, "a crashing improve must not wedge the session"


def test_dead_holder_lock_is_reclaimed():
    """A crashed worker's lock must not strand the session forever."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    c = _fresh_common(tmp)
    import hashlib

    d = hashlib.sha1(b"sess-A").hexdigest()
    p = tmp / "improve-locks" / f"{d}.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    # created_at in the far future so age alone cannot explain the reclaim — the
    # dead-pid branch must be what clears it. pid_alive is stubbed rather than
    # guessing an unused pid, so the test can't flake on a recycled pid.
    p.write_text(json.dumps({"owner": "dead", "pid": 4242, "created_at": 9e9}))
    c._proc.pid_alive = lambda pid: False
    with c.improve_session_lock("sess-A", "reclaimer") as claimed:
        assert claimed is True, "stale lock from a dead pid must be reclaimed"


def test_live_holder_lock_is_not_stolen():
    """The reclaim path must not evict a lock whose owner is still running."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    c = _fresh_common(tmp)
    import hashlib

    d = hashlib.sha1(b"sess-A").hexdigest()
    p = tmp / "improve-locks" / f"{d}.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    p.write_text(json.dumps({"owner": "live", "pid": 4242, "created_at": now}))
    c._proc.pid_alive = lambda pid: True
    with c.improve_session_lock("sess-A", "intruder") as claimed:
        assert claimed is False, "a live holder's lock must be respected"


def test_missing_session_id_never_blocks():
    tmp = pathlib.Path(tempfile.mkdtemp())
    c = _fresh_common(tmp)
    with c.improve_session_lock("", "no-id") as a:
        with c.improve_session_lock("", "also-no-id") as b:
            assert a is True and b is True, "no session id => no claim, must fail open"


def test_run_session_improve_skips_when_claim_is_held():
    """The guard lives in run_session_improve, so every caller inherits it."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    c = _fresh_common(tmp)
    called = []
    c._run_session_improve_locked = lambda ds, sid: called.append((ds, sid)) or True

    assert c.run_session_improve("ds", "sess-A") is True
    assert called == [("ds", "sess-A")], "uncontended improve must run"

    with c.improve_session_lock("sess-A", "holder"):
        assert c.run_session_improve("ds", "sess-A") is False, "must report not-synced"
    assert len(called) == 1, "contended improve must NOT reach the submit"


# --- recall always names its dataset ----------------------------------------


def _captured_recall_payload(tmp: pathlib.Path, **kwargs) -> dict:
    c = _fresh_common(tmp)
    seen = {}

    def fake_request(path, payload=None, *, method="POST", timeout=30.0):
        seen["path"], seen["payload"] = path, payload
        return []

    c._json_http_request = fake_request
    c.recall_via_http("q", session_id="s", top_k=5, scope=["graph"], **kwargs)
    return seen


def test_recall_sends_the_dataset():
    seen = _captured_recall_payload(pathlib.Path(tempfile.mkdtemp()), dataset="agent_sessions")
    assert seen["path"] == "/api/v1/recall"
    assert seen["payload"].get("datasets") == ["agent_sessions"], seen["payload"]


def test_recall_omits_datasets_key_when_no_dataset_known():
    """No dataset => no key, rather than a null/empty list the server must parse."""
    seen = _captured_recall_payload(pathlib.Path(tempfile.mkdtemp()))
    assert "datasets" not in seen["payload"], seen["payload"]


def test_recall_still_sends_search_type_and_scope():
    seen = _captured_recall_payload(
        pathlib.Path(tempfile.mkdtemp()), dataset="ds", search_type="HYBRID_COMPLETION"
    )
    p = seen["payload"]
    assert p["datasets"] == ["ds"] and p["search_type"] == "HYBRID_COMPLETION"
    assert p["scope"] == ["graph"] and p["only_context"] is True


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {_name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - report, don't mask
                failures += 1
                print(f"ERROR {_name}: {type(exc).__name__}: {exc}")
    print("\nALL PASSED" if not failures else f"\n{failures} FAILED")
    sys.exit(1 if failures else 0)
