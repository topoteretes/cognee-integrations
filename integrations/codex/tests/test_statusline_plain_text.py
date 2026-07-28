"""Guard: the Codex status line must contain NO ANSI escapes.

Claude Code renders into a terminal status bar, so it styles the `local`/`cloud`
mode bold+coloured. Codex injects this same string into the *model's* context (and
through `json.dumps` on the live status path), where an escape sequence is noise the
model has to read past. This test exists so the styling never gets copied across
integrations by symmetry.

Run: python integrations/codex/tests/test_statusline_plain_text.py (or via pytest).
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import cognee_statusline_render as sl  # noqa: E402

_LOCAL_URL = "http://127.0.0.1:8000"
_CLOUD_URL = "https://api.example-cognee.ai"


class _Mode:
    """Pin the active base_url and keep every marker out of the way."""

    def __init__(self, base_url):
        self._base_url = base_url

    def __enter__(self):
        self._dir = pathlib.Path(tempfile.mkdtemp())
        self._orig = (
            sl._CONFIG_PATH,
            sl._SERVER_READY_PATH,
            sl._BREAKER_PATH,
            sl._LLM_STATE_PATH,
            sl._LLM_STATE_DIR,
            sl._CONN_STATE_DIR,
        )
        self._orig_url = os.environ.get("COGNEE_BASE_URL")
        sl._CONFIG_PATH = self._dir / "config.json"
        sl._SERVER_READY_PATH = self._dir / "server-ready.json"
        sl._BREAKER_PATH = self._dir / "recall-breaker.json"
        sl._LLM_STATE_PATH = self._dir / "llm-state.json"
        sl._LLM_STATE_DIR = self._dir / "llm-state"
        sl._CONN_STATE_DIR = self._dir / "conn-state"
        os.environ["COGNEE_BASE_URL"] = self._base_url
        return self

    def __exit__(self, *_exc):
        (
            sl._CONFIG_PATH,
            sl._SERVER_READY_PATH,
            sl._BREAKER_PATH,
            sl._LLM_STATE_PATH,
            sl._LLM_STATE_DIR,
            sl._CONN_STATE_DIR,
        ) = self._orig
        if self._orig_url is None:
            os.environ.pop("COGNEE_BASE_URL", None)
        else:
            os.environ["COGNEE_BASE_URL"] = self._orig_url
        shutil.rmtree(self._dir, ignore_errors=True)
        return False


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_local_mode_status_is_plain():
    with _Mode(_LOCAL_URL):
        out = sl.render_status_for_host("s1")
        assert out == "cognee: agent_sessions · local", repr(out)


def test_cloud_mode_status_is_plain():
    with _Mode(_CLOUD_URL):
        out = sl.render_status_for_host("s1")
        assert out == "cognee: agent_sessions · cloud", repr(out)


def test_no_escapes_even_with_every_signal_firing():
    """Health glyph + LLM glyph + update nudge, all at once, still plain."""
    orig_update = (sl._UPDATE_CHECK_PATH, os.environ.get("COGNEE_UPDATE_CHECK"))
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        os.environ.pop("COGNEE_UPDATE_CHECK", None)
        sl._UPDATE_CHECK_PATH = tmp / "update-check.json"
        _write(
            sl._UPDATE_CHECK_PATH,
            {"update_available": True, "installed_version": "1.0.0", "latest_version": "1.1.0"},
        )
        with _Mode(_LOCAL_URL):
            _write(sl._LLM_STATE_DIR / "s1.json", {"llm_state": "not_set", "checked_at": 9e9})
            out = sl.render_status_for_host("s1")
        assert "\033" not in out, repr(out)
        assert sl._LLM_KEY_REASON in out and "update available" in out, repr(out)
    finally:
        sl._UPDATE_CHECK_PATH = orig_update[0]
        if orig_update[1] is None:
            os.environ.pop("COGNEE_UPDATE_CHECK", None)
        else:
            os.environ["COGNEE_UPDATE_CHECK"] = orig_update[1]
        shutil.rmtree(tmp, ignore_errors=True)


def test_shell_entrypoint_output_is_plain():
    """cognee-statusline.sh runs main() directly — same guarantee there."""
    with tempfile.TemporaryDirectory() as home:
        env = os.environ.copy()
        env["HOME"] = home  # POSIX
        env["USERPROFILE"] = home  # Windows: Path.home() prefers this
        env["PYTHONIOENCODING"] = "utf-8"
        env["COGNEE_BASE_URL"] = _CLOUD_URL
        env["COGNEE_UPDATE_CHECK"] = "0"
        env.pop("COGNEE_PLUGIN_DATASET", None)

        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS / "cognee_statusline_render.py")],
            input=b"{}",
            capture_output=True,
            env=env,
        )
        out = proc.stdout.decode("utf-8")
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        assert out == "cognee: agent_sessions · cloud", repr(out)


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {_name}: {exc}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
