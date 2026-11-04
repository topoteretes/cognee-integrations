"""Transport-level behaviour of _remember_http.py that no server can produce.

A read timeout must read as "the write probably landed" (non-fatal note, no CLI
fallback, no duplicate write), while a genuine connection failure is
UNREACHABLE. Both are raised at the injected-opener seam here; the wire-format
and HTTP-status half lives in integration/test_remember_http.py.

Migrated from claude-code/tests/test_remember_http.py.
"""

from __future__ import annotations

import io
import urllib.error

import pytest


@pytest.fixture
def rh(suite, isolated_modules):
    return isolated_modules(suite, "_remember_http")


def _raises(exc):
    def _open(req, timeout=None):
        raise exc

    return _open


def _remember(rh, opener):
    return rh.do_remember("http://x", "", "c", "ds", "user_context", opener=opener)


def test_background_flag_default_true(rh):
    assert rh._background_flag() == "true"


def test_background_flag_opt_out(rh, monkeypatch):
    for value in ("false", "0", "no", "off", "FALSE"):
        monkeypatch.setenv("COGNEE_REMEMBER_BACKGROUND", value)
        assert rh._background_flag() == "false"


def test_timeout_does_not_fall_back(rh):
    res = _remember(rh, _raises(TimeoutError("timed out")))
    assert res != rh.UNREACHABLE  # caller must NOT fall back to the CLI
    assert isinstance(res, dict) and "error" in res


def test_timeout_wrapped_in_urlerror_does_not_fall_back(rh):
    res = _remember(rh, _raises(urllib.error.URLError(TimeoutError("timed out"))))
    assert res != rh.UNREACHABLE
    assert isinstance(res, dict) and "error" in res


def test_connection_failure_is_unreachable(rh):
    res = _remember(rh, _raises(urllib.error.URLError("Connection refused")))
    assert res == rh.UNREACHABLE


def _capturing(bodies):
    """An opener that keeps each request body and answers 200 ``{}``."""

    def _open(req, timeout=None):
        bodies.append(req.data)
        return io.BytesIO(b"{}")

    return _open


def test_inline_text_is_sanitized_even_with_capture_off(rh, monkeypatch):
    monkeypatch.setenv("COGNEE_CAPTURE", "false")
    bodies = []
    text = "keep <private>SENTINEL</private> password=hunter2"
    res = rh.do_remember("http://x", "", text, "ds", "user_context", opener=_capturing(bodies))
    assert res == {"ok": True}
    assert b"keep [private]" in bodies[0]
    assert b"SENTINEL" not in bodies[0] and b"hunter2" not in bodies[0]


def test_file_upload_stays_byte_identical(rh, tmp_path):
    raw = b"x = '<private>SENTINEL</private>'\npassword = 'hunter2'\n\xff\xfe"
    path = tmp_path / "keep.py"
    path.write_bytes(raw)
    bodies = []
    rh.do_remember(
        "http://x", "", "", "ds", "project_docs", file_path=str(path), opener=_capturing(bodies)
    )
    assert raw in bodies[0]
