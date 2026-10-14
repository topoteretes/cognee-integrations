#!/usr/bin/env python3
"""Ask Cognee's code graph how the code works (the cognee-ask-code skill).

    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-ask-code.py" ask "<question>"

Thin entry point: the commands, output and exit statuses are documented in
``_ask_code.py``, which holds the logic so the tests can import it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _ask_code import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
