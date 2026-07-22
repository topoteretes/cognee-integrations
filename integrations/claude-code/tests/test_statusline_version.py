"""Unit tests for the statusline plugin-version segment.

Acceptance for topoteretes/cognee#3675:
- status line shows ``v<installed>`` when the plugin manifest is readable
- missing / unreadable manifest → no crash, no version segment
- pure-local read of ``CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json``

Run: python integrations/claude-code/tests/test_statusline_version.py
(or via pytest).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
from typing import Any, Iterator, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import cognee_statusline_render as sr  # noqa: E402


@contextlib.contextmanager
def _plugin_root(manifest: Optional[Any]) -> Iterator[pathlib.Path]:
    """Temp CLAUDE_PLUGIN_ROOT; ``manifest`` is JSON-serializable data or None.

    When ``manifest`` is None the ``.claude-plugin`` dir is created but no
    ``plugin.json`` is written. When ``manifest`` is a string it is written
    raw (for malformed-JSON cases).
    """
    with tempfile.TemporaryDirectory(prefix="cognee-statusline-ver-") as tmp:
        root = pathlib.Path(tmp)
        plugin_dir = root / ".claude-plugin"
        plugin_dir.mkdir()
        if manifest is not None:
            path = plugin_dir / "plugin.json"
            if isinstance(manifest, str):
                path.write_text(manifest, encoding="utf-8")
            else:
                path.write_text(json.dumps(manifest), encoding="utf-8")
        old = os.environ.get("CLAUDE_PLUGIN_ROOT")
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(root)
        try:
            yield root
        finally:
            if old is None:
                os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
            else:
                os.environ["CLAUDE_PLUGIN_ROOT"] = old


@contextlib.contextmanager
def _cleared_plugin_root() -> Iterator[None]:
    old = os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
    try:
        yield
    finally:
        if old is not None:
            os.environ["CLAUDE_PLUGIN_ROOT"] = old


def test_reads_plugin_version_from_claude_plugin_root():
    with _plugin_root({"version": "0.3.0", "name": "cognee-memory"}):
        assert sr._plugin_version() == "0.3.0"
        assert sr._version_suffix() == " · v0.3.0"


def test_missing_plugin_root_hides_version():
    with _cleared_plugin_root():
        assert sr._plugin_version() == ""
        assert sr._version_suffix() == ""


def test_missing_manifest_hides_version():
    with _plugin_root(None):
        assert sr._plugin_version() == ""
        assert sr._version_suffix() == ""


def test_malformed_manifest_hides_version():
    with _plugin_root("{"):
        assert sr._plugin_version() == ""
        assert sr._version_suffix() == ""


def test_missing_version_field_hides_version():
    with _plugin_root({"name": "cognee-memory"}):
        assert sr._plugin_version() == ""
        assert sr._version_suffix() == ""


def test_null_or_blank_version_hides_version():
    with _plugin_root({"version": None}):
        assert sr._plugin_version() == ""
    with _plugin_root({"version": "   "}):
        assert sr._plugin_version() == ""
        assert sr._version_suffix() == ""


def test_non_dict_manifest_hides_version():
    with _plugin_root(["not", "a", "dict"]):
        assert sr._plugin_version() == ""
        assert sr._version_suffix() == ""


def test_numeric_version_is_stringified():
    with _plugin_root({"version": 1}):
        assert sr._plugin_version() == "1"
        assert sr._version_suffix() == " · v1"


@contextlib.contextmanager
def _patched_main_deps():
    """Force enabled plugin and blank health/update segments for main() tests."""
    originals = {
        "plugin_enabled": sr._plugin_enabled,
        "health_prefix": sr._health_prefix,
        "update_segment": sr._update_segment,
        "stdout": sys.stdout,
        "stdin": sys.stdin,
    }
    old_dataset = os.environ.get("COGNEE_PLUGIN_DATASET")
    old_base = os.environ.get("COGNEE_BASE_URL")
    os.environ["COGNEE_PLUGIN_DATASET"] = "agent_sessions"
    os.environ.pop("COGNEE_BASE_URL", None)
    sr._plugin_enabled = lambda _cwd: True  # type: ignore[assignment]
    sr._health_prefix = lambda: ""  # type: ignore[assignment]
    sr._update_segment = lambda: ""  # type: ignore[assignment]
    buf = io.StringIO()
    sys.stdout = buf
    try:
        yield buf
    finally:
        sr._plugin_enabled = originals["plugin_enabled"]
        sr._health_prefix = originals["health_prefix"]
        sr._update_segment = originals["update_segment"]
        sys.stdout = originals["stdout"]
        sys.stdin = originals["stdin"]
        if old_dataset is None:
            os.environ.pop("COGNEE_PLUGIN_DATASET", None)
        else:
            os.environ["COGNEE_PLUGIN_DATASET"] = old_dataset
        if old_base is None:
            os.environ.pop("COGNEE_BASE_URL", None)
        else:
            os.environ["COGNEE_BASE_URL"] = old_base


def test_main_appends_version_when_plugin_enabled():
    """End-to-end: enabled plugin + manifest → version in stdout."""
    with _plugin_root({"version": "1.0.0"}):
        with _patched_main_deps() as buf:
            sys.stdin = io.StringIO("{}")
            sr.main()
            assert buf.getvalue() == "cognee: agent_sessions · local · v1.0.0"


def test_main_omits_version_when_manifest_missing():
    with _plugin_root(None):
        with _patched_main_deps() as buf:
            sys.stdin = io.StringIO("{}")
            sr.main()
            assert buf.getvalue() == "cognee: agent_sessions · local"


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
            except Exception as exc:  # pragma: no cover
                failures += 1
                print("ERROR", _name, type(exc).__name__, exc)
    sys.exit(1 if failures else 0)
