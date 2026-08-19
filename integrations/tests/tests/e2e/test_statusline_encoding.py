"""The renderer must emit UTF-8 whatever the terminal's code page is (gh #272).

The bar carries health glyphs (``●``/``✕``/``⬆``). On Windows stdout defaults to
the locale code page — typically cp1252, which cannot encode any of them — so the
write raised ``UnicodeEncodeError``, the renderer exited non-zero, and the host
dropped the *entire* status line. Losing the bar over a decorative character is a
poor trade, so ``main()`` forces UTF-8 on stdio.

The failure is reproduced portably with ``PYTHONIOENCODING=cp1252``, the same
narrow codec Windows picks by default, which makes this meaningful on any OS; the
Windows CI job then exercises the real platform on top. Stdout is compared as
**bytes** on purpose — decoding it in the parent would hide the very thing under
test, which is what the child actually wrote.

Both suites are covered because both ship this entrypoint: codex's live status
path escapes the glyphs through ``json.dumps(ensure_ascii=True)`` and so cannot
crash, but ``cognee-statusline.sh`` invokes the renderer directly, and that is
the path guarded here.

Migrated from claude-code/tests/test_statusline_render.py and
codex/tests/test_statusline_render.py, which ran only in the Windows CI job and
so never executed on Linux or macOS.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from utils.isolation import build_env
from utils.statusline import write_json
from utils.suites import plugin_root

#: U+25CF, the character from the bug report. Absent from cp1252.
_HEALTH_GLYPH = "●"


def _render_under_encoding(suite, temp_home, io_encoding: str):
    """Run the real renderer with a forced stdio encoding.

    Returns ``(returncode, stdout_bytes, stderr_text)``.
    """
    # An explicit "ready" state is what makes the renderer emit the ● prefix. An
    # empty marker reads as "unknown" and renders no glyph at all, which would
    # leave this test passing while proving nothing.
    write_json(plugin_root(temp_home) / "server-ready.json", {"state": "ready"})

    # claude-code's renderer self-evicts unless the plugin is enabled, and an
    # evicted renderer prints nothing — hiding the encoding path.
    if suite.name == "claude-code":
        write_json(
            temp_home / ".claude" / "settings.json",
            {"enabledPlugins": {"cognee-memory@cognee": True}},
        )

    # service_url=None leaves COGNEE_BASE_URL genuinely absent (local mode), and
    # build_env scrubs inherited COGNEE_*/LLM_* so a developer's shell cannot
    # change what the bar renders.
    env = build_env(suite, temp_home, service_url=None)
    env["PYTHONIOENCODING"] = io_encoding

    completed = subprocess.run(
        [sys.executable, str(suite.scripts_dir / "cognee_statusline_render.py")],
        input=b"{}",
        capture_output=True,  # bytes, deliberately not text=True
        env=env,
        cwd=str(temp_home),
        timeout=30,
    )
    return completed.returncode, completed.stdout, completed.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize(
    "io_encoding",
    [
        # The regression: the unpatched renderer died here with UnicodeEncodeError.
        pytest.param("cp1252", id="legacy-codepage"),
        # Sanity: forcing UTF-8 must not disturb the already-UTF-8 path.
        pytest.param("utf-8", id="utf-8"),
    ],
)
def test_the_health_glyph_survives_the_terminals_encoding(suite, temp_home, io_encoding):
    code, out, err = _render_under_encoding(suite, temp_home, io_encoding)

    assert code == 0, f"renderer exited {code} under {io_encoding}; stderr:\n{err}"
    assert "Traceback" not in err, f"renderer raised under {io_encoding}:\n{err}"

    # The renderer must write UTF-8 regardless of the code page it was handed.
    text = out.decode("utf-8")
    assert _HEALTH_GLYPH in text, f"health glyph missing under {io_encoding}: {text!r}"
    assert "cognee:" in text, f"status body missing under {io_encoding}: {text!r}"
