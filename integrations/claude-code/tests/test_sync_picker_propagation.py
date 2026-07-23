"""The detached session-end sync must honor the project dataset picker.

The final sync runs in a detached worker with no stdin payload, so it can't
recover the project ``cwd`` itself. ``_spawn_detached_sync(cwd)`` therefore
resolves the picker-aware dataset up front and pins it (plus ``CLAUDE_CWD``) in
the child's environment. Without this, a ``.cognee/session-config.json`` dataset
is honored all session and then silently dropped at the final flush.

Fixture-free (like the sibling claude-code tests) so it runs standalone under
plain ``python3`` as well as ``pytest``.

Run: python integrations/claude-code/tests/test_sync_picker_propagation.py (or via pytest).
"""

import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import config  # noqa: E402

_MANAGED_ATTRS = ("_CONFIG_DIR", "_STATE_DIR", "_CONFIG_FILE", "_HOOK_LOG")


def _is_managed_env(key: str) -> bool:
    # Everything the picker + detached sync read, incl. COGNEE_SYNC_DATASET
    # (not in _ENV_MAP), so a value in the developer's shell can't leak in.
    return key.startswith(("COGNEE_", "LLM_")) or key == "CLAUDE_CWD"


def _load_sync_module():
    spec = importlib.util.spec_from_file_location(
        "sync_session_to_graph", str(_SCRIPTS / "sync-session-to-graph.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _isolated():
    """Redirect config's home/state dirs into a temp tree and clear its env vars.

    Yields the project temp path; restores config globals, env, and cwd on exit.
    """
    saved_env = {k: v for k, v in os.environ.items() if _is_managed_env(k)}
    saved_attrs = {k: getattr(config, k) for k in _MANAGED_ATTRS}
    tmp = tempfile.mkdtemp()
    try:
        for k in [k for k in os.environ if _is_managed_env(k)]:
            os.environ.pop(k, None)
        home = Path(tmp) / "home" / ".cognee-plugin" / "claude-code"
        home.mkdir(parents=True)
        config._CONFIG_DIR = home
        config._STATE_DIR = home
        config._CONFIG_FILE = home / "config.json"
        config._HOOK_LOG = home / "hook.log"
        project = Path(tmp) / "project"
        project.mkdir()
        yield project
    finally:
        for k in [k for k in os.environ if _is_managed_env(k)]:
            os.environ.pop(k, None)
        os.environ.update(saved_env)
        for k, v in saved_attrs.items():
            setattr(config, k, v)
        shutil.rmtree(tmp, ignore_errors=True)


def _write_picker(project: Path, data: dict) -> None:
    (project / ".cognee").mkdir(parents=True, exist_ok=True)
    (project / ".cognee" / "session-config.json").write_text(json.dumps(data), encoding="utf-8")


@contextlib.contextmanager
def _captured_spawn(sync):
    """Stub the module's subprocess.Popen and capture the env it was launched with."""
    captured = {}
    original = sync.subprocess.Popen

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            captured["env"] = kwargs.get("env", {})

    sync.subprocess.Popen = _FakePopen
    try:
        yield captured
    finally:
        sync.subprocess.Popen = original


def test_detached_sync_pins_picked_dataset():
    with _isolated() as project:
        _write_picker(project, {"dataset": "picker-dataset"})
        sync = _load_sync_module()
        with _captured_spawn(sync) as captured:
            assert sync._spawn_detached_sync(str(project)) is True
        assert captured["env"].get("COGNEE_SYNC_DATASET") == "picker-dataset"
        assert captured["env"].get("CLAUDE_CWD") == str(project)


def test_detached_sync_does_not_override_explicit_dataset():
    with _isolated() as project:
        _write_picker(project, {"dataset": "picker-dataset"})
        # An upstream spawner already pinned a dataset — it must win (setdefault).
        os.environ["COGNEE_SYNC_DATASET"] = "explicit-dataset"
        sync = _load_sync_module()
        with _captured_spawn(sync) as captured:
            assert sync._spawn_detached_sync(str(project)) is True
        assert captured["env"].get("COGNEE_SYNC_DATASET") == "explicit-dataset"


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
