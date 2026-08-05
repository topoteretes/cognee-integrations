#!/usr/bin/env python3
"""Refresh the status-line credits marker when a turn finishes (SDK-355).

Registered on ``Stop``: the user should see what the finished turn cost as
soon as the response completes — not one prompt later. Codex does not support
async command hooks yet (an ``async: true`` hook is skipped entirely), so this
runs synchronously with a tight timeout; Codex launches matching hooks
concurrently, so the QA store on the same event does not wait on it.

The spend delta since the previous reading (normally the unlabeled
prompt-time refresh in store-user-prompt) is attributed to the turn and
rendered as ``last turn ~$X.XX``. ``refresh_credits`` never raises and no-ops
entirely on a local server, so this hook is safe to fire unconditionally.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import hook_log, quiet_hook_output, refresh_credits


def main():
    sys.stdin.read()  # consume the hook payload as the host requires
    try:
        with quiet_hook_output("credits-refresh"):
            marker = refresh_credits("turn")
        if marker:
            hook_log(
                "credits_turn_refresh",
                {"remaining_usd": marker.get("remaining_usd")},
            )
    except Exception as exc:
        hook_log("credits_refresh_error", {"error": str(exc)[:200]})


if __name__ == "__main__":
    main()
