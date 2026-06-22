#!/usr/bin/env python3
"""Print the current Cognee status line for this Codex launch.

Usage:
  python3 cognee-session-current.py
  python3 cognee-session-current.py --json
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import find_host_pid, read_host_key_for_pid  # noqa: E402
from cognee_statusline_render import render_status_for_host  # noqa: E402


def main() -> None:
    as_json = "--json" in sys.argv[1:]
    host_key = read_host_key_for_pid(find_host_pid(("codex",)))
    status_line = render_status_for_host(host_key)

    if as_json:
        print(
            json.dumps(
                {
                    "ok": bool(host_key),
                    "host_key": host_key,
                    "status_line": status_line,
                }
            )
        )
        return

    print(status_line)


if __name__ == "__main__":
    main()
