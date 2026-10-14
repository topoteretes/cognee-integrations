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
    # Graph-only on anything empty or malformed: "auto" would fold session entries in.
    assert rh.coerce_scope("not json") == ["graph"]
    assert rh.coerce_scope("") == ["graph"]


def _http_error(status, body: bytes):
    import io

    return urllib.error.HTTPError("http://x/api/v1/recall", status, "err", {}, io.BytesIO(body))


def test_rejection_names_the_dataset_and_the_servers_reason(rh):
    """The server names a dataset only by UUID; a bare status code left a model to
    guess which dataset failed and why."""
    body = (
        b'{"detail": "Dataset cf36 was embedded with \'BAAI/bge-small-en-v1.5\' '
        b'(384 dimensions), but the configured model produces 3072."}'
    )
    out = rh.do_recall(
        "http://x",
        "",
        "q",
        "",
        '["graph"]',
        "5",
        dataset="shim_testing",
        opener=_raises(_http_error(409, body)),
    )
    assert out["status"] == 409 and out["authoritative"] is False
    assert "searched dataset shim_testing" in out["error"]
    assert "384 dimensions" in out["error"]


def test_rejection_without_a_body_keeps_the_plain_message(rh):
    out = _recall(rh, _raises(_http_error(500, b"")))
    assert out["error"] == "server returned HTTP 500 for /api/v1/recall"


def _code_recall(rh, body: bytes):
    return rh.do_recall(
        "http://x",
        "",
        "q",
        "",
        '["code"]',
        "5",
        dataset="codebase-shop",
        code_query={"operation": "ask", "question": "q"},
        opener=_raises(_http_error(422, body)),
    )


def test_rejection_carries_the_servers_stable_code_and_param(rh, suite):
    """A CODE error names a stable code next to its message (SDK-1119); callers
    branch on it instead of matching the message text."""
    if suite.name != "claude-code":
        pytest.skip("the stable error fields ship in the Claude Code copy of _recall_http")
    body = (
        b'{"detail": "Unsupported CODE operation \'ask\'; expected one of architecture, '
        b'delta. [CodeSearchValidationError]", "code": "unknown_operation", '
        b'"param": "operation"}'
    )
    out = _code_recall(rh, body)
    assert out["status"] == 422 and out["authoritative"] is False
    assert out["code"] == "unknown_operation" and out["param"] == "operation"
    # The message is read from the same (single-read) body as the fields.
    assert "Unsupported CODE operation 'ask'" in out["error"]
    assert "searched dataset codebase-shop" in out["error"]


def test_rejection_from_an_older_server_keeps_the_envelope_shape(rh):
    """No code in the body (cognee 1.6.3), no new keys: current callers see no change."""
    body = b'{"detail": "Unsupported CODE operation \'ask\'. [CodeSearchValidationError]"}'
    out = _code_recall(rh, body)
    assert set(out) == {"error", "status", "authoritative"}
    assert "Unsupported CODE operation 'ask'" in out["error"]


def test_stable_fields_must_be_strings(rh, suite):
    """Only non-empty strings are passed on; anything else is not a stable code."""
    if suite.name != "claude-code":
        pytest.skip("the stable error fields ship in the Claude Code copy of _recall_http")
    out = _code_recall(rh, b'{"detail": "boom", "code": 2, "param": ""}')
    assert "code" not in out and "param" not in out and out["error"].endswith(": boom")
