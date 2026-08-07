"""Regression tests for lone-Unicode-surrogate sanitization in the capture hooks.

Host transcripts / hook payloads can legitimately contain lone surrogates (e.g.
U+DC88 from binary tool output rendered into the transcript): json.loads accepts
"\\udc88" escapes and yields a str with an unpaired surrogate. If such a string
reaches the session cache unmodified, GET /api/v1/sessions/{id} 500s with
UnicodeEncodeError during response encoding and the improve pipeline retries
forever on the same character.

These pin the pure-logic half of the capture-side fix. The whole-hook proof —
that a surrogate-carrying prompt survives a real hook run — is in
e2e/test_surrogate_sanitization.py.

Migrated from claude-code/tests/test_surrogate_sanitization.py.
"""

from __future__ import annotations

import json

import pytest

# What json.loads produces for a hook payload carrying a lone surrogate.
SURROGATE_TEXT = json.loads('{"v": "binary \\udc88 output"}')["v"]


@pytest.fixture
def store_session(suite, hook_module):
    return hook_module(suite, "store-to-session.py")


@pytest.fixture
def sanitizes(suite, request):
    """Expect failure on codex: it has no surrogate sanitization yet.

    A KNOWN BUG, not a design difference — claude-code strips lone surrogates in
    `_plugin_common._strip_surrogates` and round-trips `_truncate_str` through
    utf-8 with errors="replace"; codex has neither, so a surrogate from binary
    tool output still reaches its session cache.

    Deliberately a **strict** xfail marker rather than an imperative
    `pytest.xfail()` call: the imperative form aborts the test body, so it can
    never report XPASS and would keep silently not-testing codex forever once
    the fix lands. With strict=True the body still runs, and the moment codex is
    fixed the unexpected pass is reported as a failure — which is the signal to
    delete this fixture and its uses.
    """
    if suite.name == "codex":
        request.node.add_marker(
            pytest.mark.xfail(
                reason="codex lacks surrogate sanitization (no _strip_surrogates)",
                strict=True,
            )
        )


def test_payload_surrogate_reproduces_encode_error():
    # Sanity-check the failure mode this suite guards against.
    with pytest.raises(UnicodeEncodeError):
        SURROGATE_TEXT.encode("utf-8")


def test_truncate_str_sanitizes_untruncated_text(store_session, sanitizes):
    # The common case: text under the cap must NOT be returned verbatim.
    out = store_session._truncate_str(SURROGATE_TEXT, 4000)
    out.encode("utf-8")  # must not raise
    assert "\udc88" not in out
    assert out == "binary ? output"


def test_truncate_str_sanitizes_truncated_text(store_session):
    # Not gated: the truncation branch has always decoded the re-encoded bytes,
    # so it was already safe on both suites. Only the under-the-cap path (above)
    # returned the text verbatim.
    long_text = SURROGATE_TEXT * 500
    out = store_session._truncate_str(long_text, 100)
    out.encode("utf-8")  # must not raise
    assert out.endswith("...")
    assert "\udc88" not in out


def test_truncate_str_sanitizes_non_string_values(store_session, sanitizes):
    out = store_session._truncate_str({"nested": SURROGATE_TEXT}, 4000)
    out.encode("utf-8")  # must not raise
    assert "\udc88" not in out


def test_truncate_str_none_and_plain_text_unchanged(store_session):
    assert store_session._truncate_str(None, 100) == ""
    assert store_session._truncate_str("plain ascii", 100) == "plain ascii"
    assert store_session._truncate_str("émoji ✨", 100) == "émoji ✨"


def test_infer_status_error_message_sanitized(store_session, sanitizes):
    payload = {"tool_response": {"is_error": True, "error": SURROGATE_TEXT}}
    status, err = store_session._infer_status(payload)
    assert status == "error"
    err.encode("utf-8")  # must not raise
