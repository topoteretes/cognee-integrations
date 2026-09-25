"""Test-time environment for the Obsidian integration.

``cognee_integration_obsidian.traversal`` reads ``VAULT`` at import time and
raises ``SystemExit`` when it is unset, so the variable has to exist before the
module is imported. Point it at a scratch directory; these tests exercise the
pure parsing helpers and never touch the vault.
"""

import os
import tempfile

os.environ.setdefault("VAULT", tempfile.mkdtemp(prefix="obsidian-vault-"))
os.environ.setdefault(
    "MANIFEST_PATH", os.path.join(tempfile.gettempdir(), "cognee-manifest-test.json")
)
