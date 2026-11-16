"""``cognee_remember.py --file-size`` sizes a file for the hooks module (SDK-1411).

The module's remember tool checks a file against its limits before storing
it. It asks the script, so the module inspects no file itself. The call needs
no server and no key.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "claude-code" / "scripts" / "cognee_remember.py"


def _size(path, home) -> dict:
    ran = subprocess.run(
        [sys.executable, str(_SCRIPT), "--file-size", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout)


def test_reports_a_files_size_a_directory_and_a_missing_path(tmp_path):
    note = tmp_path / "note.txt"
    note.write_text("x" * 1234, encoding="utf-8")
    assert _size(note, tmp_path) == {"kind": "file", "size": 1234}
    assert _size(tmp_path, tmp_path) == {"kind": "directory", "size": 0}
    assert _size(tmp_path / "nope.txt", tmp_path) == {"kind": "missing", "size": 0}
