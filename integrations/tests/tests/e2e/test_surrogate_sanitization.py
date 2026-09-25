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

from utils.suites import state_dir

# What json.loads produces for a hook payload carrying a lone surrogate.
SURROGATE_TEXT = json.loads('{"v": "binary \\udc88 output"}')["v"]


def test_pending_prompt_written_by_the_hook_is_valid_utf8(
    suite, run_hook, mock_server, payloads, temp_home
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
