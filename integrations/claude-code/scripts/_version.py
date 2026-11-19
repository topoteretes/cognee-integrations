"""The plugin's version, for the scripts that report it.

Kept equal to the ``version`` in the plugin's manifest by a test
(``test_claude_version_constant.py``). The scripts read it from here so that
none of them opens the manifest.
"""

from __future__ import annotations

PLUGIN_VERSION = "1.8.11"
