"""Parity test: the Codex status line mirrors Claude Code's update badge.

Issue #3687 asked to port Claude Code's version/update surface to Codex. Both
renderers now surface an "update available" segment (``_update_segment``) from
the same ``~/.cognee-plugin/<tool>/update-check.json`` marker, differing only in
presentation: Claude Code wraps it in ANSI color for its terminal status bar,
while Codex stays plain text because it renders inside the model's context. This
test pins that behavioral parity so the two ports cannot silently drift.

Run: python integrations/codex/tests/test_statusline_version.py (or via pytest).
"""

import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_CLAUDE_SCRIPTS = _ROOT / "integrations" / "claude-code" / "scripts"
_CODEX_SCRIPTS = _ROOT / "integrations" / "codex" / "plugins" / "cognee" / "scripts"
_CLAUDE = _CLAUDE_SCRIPTS / "cognee_statusline_render.py"
_CODEX = _CODEX_SCRIPTS / "cognee_statusline_render.py"

# Match a real ANSI SGR escape (ESC = 0x1b), not the literal characters "\033".
_ANSI = re.compile("\x1b\\[[0-9;]*m")

_UPDATE = {"update_available": True, "installed_version": "1.0.0", "latest_version": "2.0.0"}


def _load(name, path):
    # Distinct module names: both files share the basename cognee_statusline_render,
    # so a plain ``import`` would return the first copy from sys.modules and end up
    # comparing a module to itself.
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


claude = _load("claude_render", _CLAUDE)
codex = _load("codex_render", _CODEX)


def _segment(mod, marker, *, opt_out=False):
    """Render mod._update_segment() with `marker` (a dict, or None for no file).

    ``_UPDATE_CHECK_PATH`` is a module constant frozen at import, so we point it at
    a temp file rather than patching Path.home(); env + constant are restored after.
    """
    saved_path = mod._UPDATE_CHECK_PATH
    saved_env = os.environ.get("COGNEE_UPDATE_CHECK")
    with tempfile.TemporaryDirectory() as tmp:
        marker_path = pathlib.Path(tmp) / "update-check.json"
        if marker is not None:
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
        mod._UPDATE_CHECK_PATH = marker_path
        if opt_out:
            os.environ["COGNEE_UPDATE_CHECK"] = "0"
        else:
            os.environ.pop("COGNEE_UPDATE_CHECK", None)
        try:
            return mod._update_segment()
        finally:
            mod._UPDATE_CHECK_PATH = saved_path
            if saved_env is None:
                os.environ.pop("COGNEE_UPDATE_CHECK", None)
            else:
                os.environ["COGNEE_UPDATE_CHECK"] = saved_env


def _core(segment):
    """Reduce a rendered segment to its shared message body (strip ANSI + spaces)."""
    return _ANSI.sub("", segment).strip()


def test_no_marker_both_empty():
    assert _segment(claude, None) == ""
    assert _segment(codex, None) == ""


def test_update_available_same_core_message():
    c, x = _segment(claude, _UPDATE), _segment(codex, _UPDATE)
    assert c and x  # both surface a badge
    assert _core(c) == _core(x) == "⬆ Cognee update available 1.0.0→2.0.0"


def test_presentation_difference_preserved():
    # The one deliberate divergence: Claude Code colors for its bar; Codex is plain.
    assert "\x1b[" in _segment(claude, _UPDATE)
    assert "\x1b[" not in _segment(codex, _UPDATE)


def test_update_flag_false_both_empty():
    marker = {"update_available": False, "installed_version": "1.0.0", "latest_version": "2.0.0"}
    assert _segment(claude, marker) == ""
    assert _segment(codex, marker) == ""


def test_missing_latest_version_both_empty():
    marker = {"update_available": True, "installed_version": "1.0.0"}
    assert _segment(claude, marker) == ""
    assert _segment(codex, marker) == ""


def test_env_opt_out_both_empty():
    assert _segment(claude, _UPDATE, opt_out=True) == ""
    assert _segment(codex, _UPDATE, opt_out=True) == ""


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
