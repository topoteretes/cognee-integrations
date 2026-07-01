"""Tests for the compact `status` one-liner: `mode=… url=… key=set|missing version=…`.

The line reuses the same runtime resolvers the hooks run with and must never
print the API key value — only whether one is set.

Run: python integrations/codex/plugins/cognee/tests/test_status.py
"""

import contextlib
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402


@contextlib.contextmanager
def _patched(runtime, plugin_version="1.0.3-local", server_version=""):
    """Swap the resolvers so the line is deterministic regardless of host env."""
    saved = (pc.resolve_runtime_mode, pc._plugin_version, pc._server_version)
    pc.resolve_runtime_mode = lambda: dict(runtime)
    pc._plugin_version = lambda: plugin_version
    pc._server_version = lambda: server_version
    try:
        yield
    finally:
        pc.resolve_runtime_mode, pc._plugin_version, pc._server_version = saved


def _fields(line):
    return dict(part.split("=", 1) for part in line.split(" "))


def test_shape_and_masks_key_when_present():
    runtime = {"mode": "http", "base_url": "http://localhost:8011", "api_key_present": True}
    with _patched(runtime):
        line = pc.runtime_status_line()
    assert "\n" not in line
    assert _fields(line) == {
        "mode": "http",
        "url": "http://localhost:8011",
        "key": "set",
        "version": "1.0.3-local",
    }


def test_reports_missing_key():
    runtime = {"mode": "local_sdk", "base_url": "", "api_key_present": False}
    with _patched(runtime):
        assert _fields(pc.runtime_status_line())["key"] == "missing"


def test_appends_server_version_when_known():
    runtime = {"mode": "http", "base_url": "http://localhost:8011", "api_key_present": False}
    with _patched(runtime, server_version="1.2.2"):
        fields = _fields(pc.runtime_status_line())
    assert fields["version"] == "1.0.3-local"
    assert fields["server"] == "1.2.2"


def test_never_prints_raw_key():
    """End-to-end through the real resolver: an env key must stay masked."""
    secret = "sk-super-secret-value"
    os.environ["COGNEE_API_KEY"] = secret
    try:
        line = pc.runtime_status_line()
    finally:
        os.environ.pop("COGNEE_API_KEY", None)
    assert secret not in line
    assert "key=set" in line


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
