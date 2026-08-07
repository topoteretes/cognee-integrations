"""End-to-end proof that a lone surrogate cannot get through a capture hook.

The bug this guards is a *serialization* failure: an unpaired surrogate that
reaches the session cache makes the server 500 with UnicodeEncodeError while
encoding its response, and the improve pipeline then retries the same character
forever. Sanitizing at capture time is the fix.

Running the real hook as a subprocess is a strictly better proof than asserting
on a captured lambda argument: the prompt has to survive the hook's own JSON
serialization and file write, which is exactly where an unstripped surrogate
raises. If sanitization regresses, the hook exits non-zero or leaves a file
nothing can read.

Migrated from claude-code/tests/test_surrogate_sanitization.py.
"""

from __future__ import annotations

import json

import pytest
from utils.suites import state_dir

# What json.loads produces for a hook payload carrying a lone surrogate.
SURROGATE_TEXT = json.loads('{"v": "binary \\udc88 output"}')["v"]


@pytest.fixture
def sanitizes(suite, request):
    """Expect failure on codex — a KNOWN BUG, not a design difference.

    codex has no `_strip_surrogates`, so the surrogate survives into its
    pending-prompt file (written with ensure_ascii, hence no local write error)
    and only blows up later, server-side.

    Strict marker, not an imperative `pytest.xfail()`: the body must still run so
    that fixing codex reports an unexpected pass instead of silently leaving the
    test inert. That failure is the signal to delete this gate.
    """
    if suite.name == "codex":
        request.node.add_marker(
            pytest.mark.xfail(
                reason="codex lacks surrogate sanitization (no _strip_surrogates)",
                strict=True,
            )
        )


def test_pending_prompt_written_by_the_hook_is_valid_utf8(
    suite, run_hook, mock_server, payloads, temp_home, sanitizes
):
    result = run_hook(
        suite,
        "store-user-prompt.py",
        stdin=payloads.user_prompt(prompt=SURROGATE_TEXT, turn_id="t1"),
        service_url=mock_server.url,
    )
    assert result.returncode == 0, result.stderr

    pending = list((state_dir(suite, temp_home) / "pending").glob("*.json"))
    assert pending, "the hook wrote no pending-prompt file"

    for path in pending:
        # Reading as strict UTF-8 is the assertion: an unstripped surrogate
        # could not have been written here in the first place.
        stored = json.loads(path.read_text(encoding="utf-8"))
        for entry in stored.values():
            prompt = entry.get("prompt", "")
            prompt.encode("utf-8")  # must not raise
            assert "\udc88" not in prompt
            assert prompt == "binary ? output"
