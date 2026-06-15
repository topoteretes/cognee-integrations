"""Pytest bootstrap for the integration test suites.

Makes the ``utils`` package importable and registers its fixtures as a plugin so
the shared infrastructure is available to tests under integrations/tests/.
"""

import sys
from pathlib import Path

# Ensure `import utils...` resolves regardless of the pytest invocation cwd.
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

pytest_plugins = ("utils.fixtures",)
