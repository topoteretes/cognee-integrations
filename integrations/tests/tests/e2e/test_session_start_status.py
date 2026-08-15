"""SessionStart status envelope.

Codex-compatible hook status belongs in top-level ``systemMessage``. Keep the
same text in ``additionalContext`` so it is still available as model context.
"""

from __future__ import annotations

import json

import pytest


def test_codex_session_start_emits_top_level_status(
    suite, run_hook, mock_server, payloads, project_dir
):
    if suite.name != "codex":
        pytest.skip(f"{suite.name}: statusMessage is nested in hookSpecificOutput")

    result = run_hook(
        suite,
        "session-start.py",
        stdin=payloads.session_start(cwd=str(project_dir)),
        service_url=mock_server.url,
    )
    assert result.returncode == 0, result.stderr

    output = json.loads(result.stdout.strip())
    assert output["systemMessage"].startswith("● cognee:")
    assert output["hookSpecificOutput"]["additionalContext"].startswith(output["systemMessage"])
    assert "systemMessage" not in output["hookSpecificOutput"]
