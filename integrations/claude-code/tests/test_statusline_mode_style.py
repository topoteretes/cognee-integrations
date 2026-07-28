"""Tests for `_mode_label` (cognee_statusline_render.py) — the styled `local`/`cloud`
word in the status line.

Where memory actually lives is the one thing in the bar worth a double-take, so the
mode is bold + coloured: cyan for `local`, magenta for `cloud`. Red/green/yellow are
already spoken for by the health glyph and the amber warnings, and bold *and* colour
are set together so a terminal that ignores one still shows the other.

Locked down here: the two modes are visually distinct, the plain `_active_mode()`
control value is untouched (it gates the LLM glyph, so styling it would break that),
and every sequence is reset so no colour bleeds into the counts that follow.

Run: python integrations/claude-code/tests/test_statusline_mode_style.py
(or via pytest).
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import cognee_statusline_render as sl  # noqa: E402

_LOCAL_URL = "http://127.0.0.1:8000"
_CLOUD_URL = "https://api.example-cognee.ai"
_RESET = "\033[0m"


class _Mode:
    """Pin the active base_url (and keep the config file out of the way)."""

    def __init__(self, base_url):
        self._base_url = base_url

    def __enter__(self):
        self._dir = pathlib.Path(tempfile.mkdtemp())
        self._orig_cfg = sl._CONFIG_PATH
        self._orig_url = os.environ.get("COGNEE_BASE_URL")
        sl._CONFIG_PATH = self._dir / "config.json"  # never written
        if self._base_url is None:
            os.environ.pop("COGNEE_BASE_URL", None)
        else:
            os.environ["COGNEE_BASE_URL"] = self._base_url
        return self

    def __exit__(self, *_exc):
        sl._CONFIG_PATH = self._orig_cfg
        if self._orig_url is None:
            os.environ.pop("COGNEE_BASE_URL", None)
        else:
            os.environ["COGNEE_BASE_URL"] = self._orig_url
        shutil.rmtree(self._dir, ignore_errors=True)
        return False


# ── each mode gets its own style ───────────────────────────────────────────


def test_local_is_bold_cyan():
    with _Mode(_LOCAL_URL):
        assert sl._mode_label() == f"\033[1;36mlocal{_RESET}"


def test_cloud_is_bold_magenta():
    with _Mode(_CLOUD_URL):
        assert sl._mode_label() == f"\033[1;35mcloud{_RESET}"


def test_unset_base_url_is_styled_local():
    with _Mode(None):
        assert sl._mode_label() == f"\033[1;36mlocal{_RESET}"


def test_the_two_modes_are_visually_distinct():
    with _Mode(_LOCAL_URL):
        local = sl._mode_label()
    with _Mode(_CLOUD_URL):
        cloud = sl._mode_label()
    assert local != cloud
    assert local.split("m")[0] != cloud.split("m")[0], "same colour code for both modes"


def test_both_modes_are_bold_and_coloured():
    """A terminal that drops bold must still show colour, and vice versa."""
    for url in (_LOCAL_URL, _CLOUD_URL):
        with _Mode(url):
            label = sl._mode_label()
        assert label.startswith("\033[1;"), label  # 1 = bold
        assert label[:-4].split(";")[1].startswith("3"), label  # 3x = foreground colour


# ── the sequences are well-formed ─────────────────────────────────────────


def test_style_is_reset_so_nothing_bleeds_into_the_counts():
    with _Mode(_LOCAL_URL):
        assert sl._mode_label().endswith(_RESET)


def test_the_word_itself_is_unchanged():
    """Whatever the styling, the text a user reads must still say local/cloud."""
    for url, word in ((_LOCAL_URL, "local"), (_CLOUD_URL, "cloud")):
        with _Mode(url):
            assert word in sl._mode_label().replace("\033", "")


def test_active_mode_stays_plain():
    """It is a control value too — `_llm_prefix` compares it to "local"."""
    for url, word in ((_LOCAL_URL, "local"), (_CLOUD_URL, "cloud")):
        with _Mode(url):
            assert sl._active_mode() == word
            assert "\033" not in sl._active_mode()


# ── end to end ────────────────────────────────────────────────────────────


def test_full_render_styles_the_mode_in_place():
    with tempfile.TemporaryDirectory() as home:
        home_path = pathlib.Path(home)
        (home_path / ".cognee-plugin" / "claude-code").mkdir(parents=True)
        claude_dir = home_path / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(
            '{"enabledPlugins": {"cognee-memory@cognee": true}}', encoding="utf-8"
        )

        env = os.environ.copy()
        env["HOME"] = home  # POSIX
        env["USERPROFILE"] = home  # Windows: Path.home() prefers this
        env["PYTHONIOENCODING"] = "utf-8"
        env["COGNEE_BASE_URL"] = _CLOUD_URL
        env["COGNEE_UPDATE_CHECK"] = "0"
        env.pop("COGNEE_PLUGIN_DATASET", None)

        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS / "cognee_statusline_render.py")],
            input=json.dumps({"session_id": "s1"}).encode("utf-8"),
            capture_output=True,
            env=env,
        )
        out = proc.stdout.decode("utf-8")
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        assert out == f"cognee: agent_sessions · \033[1;35mcloud{_RESET}", repr(out)


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
