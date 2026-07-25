"""Regression tests for the status-line renderer's stdio encoding.

The renderer prints health glyphs (``●``/``✕``/``⬆``) to stdout. On Windows,
stdout defaults to the locale code page (e.g. cp1252), which cannot encode
those characters: the write raises ``UnicodeEncodeError``, the renderer exits
non-zero, and Claude Code drops the whole status line (gh #272). ``main()``
forces UTF-8 on stdio to prevent that.

We reproduce the failure portably by launching the renderer as a subprocess
with ``PYTHONIOENCODING=cp1252`` — the same narrow codec Windows uses by
default — so the test is meaningful on any OS (the Windows CI runner exercises
the real platform too). Run: `python integrations/claude-code/tests/test_statusline_render.py`
(or via pytest).
"""

import os
import pathlib
import subprocess
import sys
import tempfile

_RENDERER = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "cognee_statusline_render.py"
_HEALTH_GLYPH = "●"  # ● — the U+25CF from the bug report; absent from cp1252


def _run_under_encoding(io_encoding: str):
    """Run the renderer with a forced stdio encoding and a fake, plugin-enabled
    HOME, returning ``(returncode, stdout_bytes, stderr_text)``."""
    with tempfile.TemporaryDirectory() as home:
        home_path = pathlib.Path(home)
        # server-ready.json makes the renderer emit the ● health prefix — the
        # exact character that cannot be encoded under cp1252.
        (home_path / ".cognee-plugin").mkdir(parents=True)
        (home_path / ".cognee-plugin" / "server-ready.json").write_text("{}", encoding="utf-8")
        # Enable the plugin in user settings so the renderer does not self-evict
        # (which would render nothing and hide the encoding path under test).
        claude_dir = home_path / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(
            '{"enabledPlugins": {"cognee-memory@cognee": true}}', encoding="utf-8"
        )

        env = os.environ.copy()
        env["HOME"] = home  # POSIX
        env["USERPROFILE"] = home  # Windows: Path.home() prefers this
        env["PYTHONIOENCODING"] = io_encoding
        env["COGNEE_UPDATE_CHECK"] = "0"  # suppress the update segment
        env.pop("COGNEE_BASE_URL", None)  # -> local mode
        env.pop("COGNEE_PLUGIN_DATASET", None)  # -> default dataset

        proc = subprocess.run(
            [sys.executable, str(_RENDERER)],
            input=b"{}",
            capture_output=True,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace")


def _assert_renders_glyph(io_encoding: str):
    code, out, err = _run_under_encoding(io_encoding)
    assert code == 0, f"renderer exited {code} under {io_encoding}; stderr:\n{err}"
    assert "Traceback" not in err, f"renderer raised under {io_encoding}:\n{err}"
    text = out.decode("utf-8")  # renderer must emit UTF-8 regardless of the codepage
    assert _HEALTH_GLYPH in text, f"health glyph missing under {io_encoding}: {text!r}"
    assert "cognee:" in text, f"status body missing under {io_encoding}: {text!r}"


def test_glyph_renders_under_legacy_codepage():
    # The regression: on cp1252 the unpatched renderer crashes with
    # UnicodeEncodeError on U+25CF and Claude Code drops the status line.
    _assert_renders_glyph("cp1252")


def test_glyph_renders_under_utf8():
    # Sanity: the UTF-8 forcing must not disturb the normal (already-UTF-8) path.
    _assert_renders_glyph("utf-8")


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
