"""Transport-level behaviour of _remember_http.py that no server can produce.

A read timeout must read as "the write probably landed" (non-fatal note, no CLI
fallback, no duplicate write), while a genuine connection failure is
UNREACHABLE. Both are raised at the injected-opener seam here; the wire-format
and HTTP-status half lives in integration/test_remember_http.py.

Migrated from claude-code/tests/test_remember_http.py.
"""

from __future__ import annotations

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
