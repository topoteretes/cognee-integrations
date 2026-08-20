#!/usr/bin/env python3
"""Print the resolved Cognee plugin runtime state as one line, then exit 0.

    mode=<http|local_sdk> url=<service-url> key=<set|missing> version=<plugin-version>

A quick "what am I actually running with?" check. Reuses the same resolvers the
hooks run with (see ``_plugin_common.resolve_runtime_mode``) so the reported
state matches real runtime behaviour. The API key value is never printed — only
whether one is set. Pure-local: reads env vars and ``~/.cognee-plugin`` files,
makes no network call.

Run: python3 integrations/codex/plugins/cognee/scripts/cognee-status.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import runtime_status_line


def main() -> int:
    sys.stdout.write(runtime_status_line() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
