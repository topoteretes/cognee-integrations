"""Transport-exception classification for _recall_http.py.

These stay at the injected-opener seam on purpose: no HTTP server can produce
a DNS failure, an SSL handshake error, or a connection reset on demand, and the
verdict taxonomy (DOWN / SLOW / UNKNOWN) is what decides whether the wrapper is
allowed to fall back to the local CLI. The wire-level half of this module's
tests lives in integration/test_recall_http.py.

Errnos come from the ``errno`` module, never as literals: the numbers differ by
platform, and only one side of this comparison is portable. ``EHOSTUNREACH`` is 65
on macOS/BSD but 113 on Linux — where 65 is ``ENOPKG`` and therefore *correctly*
classified UNKNOWN. The classifier was always right (it builds its set from
``errno.*``); the hardcoded 65 was the bug, and it only surfaced once this suite
started running on Linux CI, having previously lived in ``claude-code/tests/``
where no CI job ever ran it.

Migrated from claude-code/tests/test_recall_http.py.
"""

from __future__ import annotations

import errno
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
    exc = urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, "no such host"))
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
        # Matched by exception class, so the errno is incidental — but it is still
        # taken from `errno` so the case reads as the failure it represents.
        (ConnectionRefusedError(errno.ECONNREFUSED, "refused"), rh.DOWN),
        (socket.gaierror(socket.EAI_NONAME, "no such host"), rh.DOWN),
        # A bare OSError is the one case decided purely by errno, so this is the
        # case that must use the symbol rather than a number.
        (OSError(errno.EHOSTUNREACH, "no route to host"), rh.DOWN),
        (OSError(errno.ENETUNREACH, "network unreachable"), rh.DOWN),
        (TimeoutError("timed out"), rh.SLOW),
        (urllib.error.URLError(TimeoutError("timed out")), rh.SLOW),
        (urllib.error.URLError("timed out"), rh.SLOW),  # string reason
        (urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "x")), rh.DOWN),
        # A reset means something answered and then dropped it — not an absent
        # server, so it must not license a CLI fallback.
        (ConnectionResetError(errno.ECONNRESET, "reset"), rh.UNKNOWN),
        (ValueError("bug in our own code"), rh.UNKNOWN),
        (ssl.SSLError("handshake"), rh.UNKNOWN),
    ]
    for exc, want in cases:
        got = rh.classify_transport_exception(exc)
        assert got == want, f"{exc!r}: want {want}, got {got}"


def test_an_unroutable_errno_is_down_on_this_platform(rh):
    """Guards the assumption the hardcoded-65 bug violated.

    The classifier builds its down-set from ``errno.*``, so it is portable by
    construction. This pins that the *test suite* agrees with the platform it is
    running on, which is what a literal errno silently broke on Linux.
    """
    for name in ("ECONNREFUSED", "EHOSTUNREACH", "ENETUNREACH"):
        code = getattr(errno, name)
        assert rh.classify_transport_exception(OSError(code, name)) == rh.DOWN, (
            f"{name}={code} is not classified down on {socket.gethostname()!r}'s platform"
        )


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
