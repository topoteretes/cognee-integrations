"""The `local` / `cloud` mode word in the status line.

`_active_mode()` is shared and must stay PLAIN: it is a control value that
`_llm_prefix` compares against "local", so styling it would break the LLM glyph.

How the word is then *presented* is the deliberate per-suite split. claude-code
renders it through `_mode_label()`: bold + coloured (cyan local, magenta cloud),
because where memory actually lives is the one thing in the bar worth a
double-take. Red/green/yellow are already spoken for by the health glyph and the
amber warnings, and bold *and* colour are set together so a terminal that
ignores one still shows the other. codex has no `_mode_label` at all — its bar
goes into the model's context and stays plain text.

Migrated from claude-code/tests/test_statusline_mode_style.py and the
render-level assertions in codex/tests/test_statusline_plain_text.py.
"""

from __future__ import annotations

import pytest

_LOCAL_URL = "http://127.0.0.1:8000"
_CLOUD_URL = "https://api.example-cognee.ai"
_RESET = "\033[0m"


# ── the control value is shared and never styled ───────────────────────────


@pytest.mark.parametrize(("base_url", "expected"), [(_LOCAL_URL, "local"), (_CLOUD_URL, "cloud")])
def test_active_mode_stays_plain(statusline, monkeypatch, base_url, expected):
    monkeypatch.setenv("COGNEE_BASE_URL", base_url)
    assert statusline._active_mode() == expected
    assert "\033" not in statusline._active_mode()


def test_unset_base_url_reads_as_local(statusline):
    """The harness scrubs COGNEE_BASE_URL, so this is the true default."""
    assert statusline._active_mode() == "local"


# ── claude-code: the word is styled ────────────────────────────────────────


@pytest.fixture
def styled(suite, statusline):
    if not hasattr(statusline, "_mode_label"):
        pytest.skip(f"{suite.name}: the bar is plain text by design (model context)")
    return statusline


def test_local_is_bold_cyan(styled, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", _LOCAL_URL)
    assert styled._mode_label() == f"\033[1;36mlocal{_RESET}"


def test_cloud_is_bold_magenta(styled, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", _CLOUD_URL)
    assert styled._mode_label() == f"\033[1;35mcloud{_RESET}"


def test_unset_base_url_is_styled_local(styled):
    assert styled._mode_label() == f"\033[1;36mlocal{_RESET}"


def test_the_two_modes_are_visually_distinct(styled, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", _LOCAL_URL)
    local = styled._mode_label()
    monkeypatch.setenv("COGNEE_BASE_URL", _CLOUD_URL)
    cloud = styled._mode_label()
    assert local != cloud
    assert local.split("m")[0] != cloud.split("m")[0], "same colour code for both modes"


@pytest.mark.parametrize("base_url", [_LOCAL_URL, _CLOUD_URL])
def test_both_modes_are_bold_and_coloured(styled, monkeypatch, base_url):
    """A terminal that drops bold must still show colour, and vice versa."""
    monkeypatch.setenv("COGNEE_BASE_URL", base_url)
    label = styled._mode_label()
    assert label.startswith("\033[1;"), label  # 1 = bold
    assert label[:-4].split(";")[1].startswith("3"), label  # 3x = foreground colour


def test_style_is_reset_so_nothing_bleeds_into_the_counts(styled, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", _LOCAL_URL)
    assert styled._mode_label().endswith(_RESET)


@pytest.mark.parametrize(("base_url", "word"), [(_LOCAL_URL, "local"), (_CLOUD_URL, "cloud")])
def test_the_word_itself_is_unchanged(styled, monkeypatch, base_url, word):
    """Whatever the styling, the text a user reads must still say local/cloud."""
    monkeypatch.setenv("COGNEE_BASE_URL", base_url)
    assert word in styled._mode_label().replace("\033", "")


# ── codex: the whole host-facing string is plain ───────────────────────────


@pytest.fixture
def plain(suite, statusline):
    if not hasattr(statusline, "render_status_for_host"):
        pytest.skip(f"{suite.name}: no host-facing emitter (the bar is terminal-only)")
    return statusline


@pytest.mark.parametrize(("base_url", "word"), [(_LOCAL_URL, "local"), (_CLOUD_URL, "cloud")])
def test_host_status_is_plain(plain, suite, monkeypatch, base_url, word):
    monkeypatch.setenv("COGNEE_BASE_URL", base_url)
    out = plain.render_status_for_host("s1")
    assert out == f"cognee: {suite.default_dataset} · {word}", repr(out)


def test_no_escapes_even_with_every_signal_firing(plain, suite, monkeypatch):
    """Health glyph + LLM glyph + update nudge, all at once, still plain."""
    from utils.statusline import write_json

    monkeypatch.setenv("COGNEE_BASE_URL", _LOCAL_URL)
    monkeypatch.delenv("COGNEE_UPDATE_CHECK", raising=False)
    # installed_version must be the RUNNING version, else the staleness guard in
    # _update_segment suppresses the nudge (see _running_plugin_version).
    write_json(
        plain._UPDATE_CHECK_PATH,
        {
            "update_available": True,
            "installed_version": plain._running_plugin_version(),
            "latest_version": "99.0.0",
        },
    )
    write_json(plain._LLM_STATE_DIR / "s1.json", {"llm_state": "not_set", "checked_at": 9e9})

    out = plain.render_status_for_host("s1")
    assert "\033" not in out, repr(out)
    assert plain._LLM_KEY_REASON in out and "update available" in out, repr(out)
