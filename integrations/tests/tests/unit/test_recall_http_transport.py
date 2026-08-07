"""Transport-exception classification for _recall_http.py.

These stay at the injected-opener seam on purpose: no HTTP server can produce
a DNS failure, an SSL handshake error, or a connection reset on demand, and the
verdict taxonomy (DOWN / SLOW / UNKNOWN) is what decides whether the wrapper is
allowed to fall back to the local CLI. The wire-level half of this module's
tests lives in integration/test_recall_http.py.

Migrated from claude-code/tests/test_recall_http.py.
"""

from __future__ import annotations

import socket
import ssl
import urllib.error

import pytest


@pytest.fixture
def rh(suite, isolated_modules):
    return isolated_modules(suite, "_recall_http")


def _raises(exc):
    def _opener(req, timeout=None):
        raise exc

    return _opener


def _recall(rh, opener):
    return rh.do_recall("http://x", "", "q", "", '["graph"]', "5", opener=opener)


def test_dns_failure_is_unreachable(rh):
    exc = urllib.error.URLError(socket.gaierror(8, "no such host"))
    assert _recall(rh, _raises(exc)) == rh.UNREACHABLE


def test_timeout_is_transient_envelope_not_unreachable(rh):
    """A timeout is 'alive but busy' — no CLI fallback, no breaker failure."""
    for exc in (TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))):
        out = _recall(rh, _raises(exc))
        assert out != rh.UNREACHABLE
        assert isinstance(out, dict) and out.get("transient") is True
        assert out["authoritative"] is False


def test_unclassifiable_error_is_transient_envelope(rh):
    """SSL trouble / vague reasons / our own bugs must not read as 'server down'."""
    out = _recall(rh, _raises(urllib.error.URLError("weird")))
    assert out != rh.UNREACHABLE
    assert isinstance(out, dict) and out.get("transient") is True


def test_classifier_verdicts(rh):
    cases = [
        (ConnectionRefusedError(61, "refused"), rh.DOWN),
        (socket.gaierror(8, "no such host"), rh.DOWN),
        (OSError(65, "no route to host"), rh.DOWN),  # EHOSTUNREACH
        (TimeoutError("timed out"), rh.SLOW),
        (urllib.error.URLError(TimeoutError("timed out")), rh.SLOW),
        (urllib.error.URLError("timed out"), rh.SLOW),  # string reason
        (urllib.error.URLError(ConnectionRefusedError(61, "x")), rh.DOWN),
        (ConnectionResetError(54, "reset"), rh.UNKNOWN),
        (ValueError("bug in our own code"), rh.UNKNOWN),
        (ssl.SSLError("handshake"), rh.UNKNOWN),
    ]
    for exc, want in cases:
        got = rh.classify_transport_exception(exc)
        assert got == want, f"{exc!r}: want {want}, got {got}"


def test_coerce_top_k(rh):
    assert rh.coerce_top_k("abc") == 5
    assert rh.coerce_top_k("0") == 5
    assert rh.coerce_top_k("") == 5
    assert rh.coerce_top_k(None) == 5
    assert rh.coerce_top_k("10") == 10


def test_coerce_scope(rh):
    assert rh.coerce_scope('["graph"]') == ["graph"]
    assert rh.coerce_scope("not json") == "auto"
    assert rh.coerce_scope("") == "auto"
