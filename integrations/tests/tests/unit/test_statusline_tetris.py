"""SDK-1109: rendering contract and frozen 1.6.21 compatibility oracle."""

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from utils.isolation import build_env
from utils.statusline import write_json

NOW = 1_800_000_000
SESSION = "test-session"


@pytest.fixture
def sl(suite, statusline, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("Claude Code presentation only")
    monkeypatch.delenv("COGNEE_STATUSLINE_STYLE", raising=False)
    # The developer's terminal may advertise truecolor; tests opt in explicitly.
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "manual")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://memory.example.test")
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setattr(statusline, "_unicode_output", lambda: True)
    monkeypatch.setattr(statusline.time, "time", lambda: NOW)
    write_json(statusline._USER_SETTINGS, {"enabledPlugins": {"cognee-memory@cognee": True}})
    write_json(
        statusline._SERVER_READY_PATH,
        {
            "state": "ready",
            "checked_at": NOW,
            "ready_at": NOW,
            "base_url": "https://memory.example.test",
        },
    )
    write_json(
        statusline._SESSIONS_DIR / SESSION / "stats.json",
        {"entries_injected": 124, "chars_injected": 1440, "recalls": 2},
    )
    write_json(statusline._RECALL_DIR / f"{SESSION}.json", marker())
    return statusline


def marker(**changes):
    return {
        "session_id": "server-session",
        "session_key": SESSION,
        "ts": NOW - 20,
        "hits": {"session": 8},
        "session_totals": {"turns": 46, "turns_with_hits": 34},
        **changes,
    }


def plain(sl, line):
    return sl._ANSI.sub("", line)


@pytest.mark.parametrize("theme,color", [("dark", 141), ("light", 93)])
def test_palette_and_meaning(sl, monkeypatch, theme, color):
    monkeypatch.setenv("COGNEE_STATUSLINE_THEME", theme)
    monkeypatch.setenv("COLUMNS", "120")
    line = sl._tetris_line(SESSION)
    # The wordmark in the brand purple, bold, with no chip around it: the same
    # row the mod draws above the prompt.
    assert f"\033[38;5;{color}m\033[1mcognee\033[0m" in line
    assert "■" not in plain(sl, line) and "\033[48;5;" not in line
    # The connection glyph leads: ● once confirmed, ○ while unknown (a failure
    # leads with ✕ and its reason); never "?".
    assert plain(sl, line).startswith(("● cognee ", "○ cognee ", "✕"))
    assert "?" not in plain(sl, line)
    assert "cognee+" not in line
    # The dataset is plain, as in the picker above the prompt; the mode purple.
    assert "cognee\033[0m manual" in line and f"\033[38;5;{color}m\033[1mmanual" not in line
    assert f"\033[38;5;{color}m\033[1mcloud" in line
    assert "manual  ·  cloud" in plain(sl, line)
    assert "tokens saved" not in line
    assert "reminiscence" not in line
    # The per-prompt count and the turn ratio are not shown.
    assert "ideas" not in line and "imprints" not in line and "▰" not in line
    assert not any(c in plain(sl, line) for c in "[]▁▂▃▄▅▆▇█▔▀")


@pytest.mark.parametrize("hits", [0, 1, 8])
def test_brand_is_constant_across_recall_outcomes(sl, monkeypatch, hits):
    monkeypatch.setenv("COLUMNS", "120")
    write_json(sl._RECALL_DIR / f"{SESSION}.json", marker(hits={"session": hits}))
    line = sl._tetris_line(SESSION)
    assert re.search(r"^[●○] cognee manual", plain(sl, line))
    assert "\033[38;5;141m\033[1mcognee\033[0m" in line
    assert "cognee+" not in line
    assert "idea" not in line


def test_width(sl, monkeypatch):
    at80 = sl._tetris_line(SESSION)
    assert sl._width(at80) <= 80
    monkeypatch.setenv("COLUMNS", "120")
    wide = sl._tetris_line(SESSION)
    assert sl._width(wide) <= 120
    monkeypatch.setenv("COLUMNS", "60")
    line = sl._tetris_line(SESSION)
    assert sl._width(line) <= 60
    assert all(text in line for text in ("cognee", "manual", "cloud"))
    monkeypatch.setenv("COLUMNS", "20")
    assert sl._width(sl._tetris_line(SESSION)) <= 20


@pytest.mark.parametrize("mode", ["full", "false"])
def test_counts_modes(sl, monkeypatch, mode):
    monkeypatch.setenv("COGNEE_STATUSLINE_COUNTS", mode)
    monkeypatch.setenv("COLUMNS", "120")
    line = sl._tetris_line(SESSION)
    # The counts switch is the classic line's: the badge line shows neither
    # the per-scope strip nor the turn ratio in any mode.
    assert "tokens saved" not in line
    assert "recall 8s/0t/0g/0a" not in line and "imprints" not in line and "▰" not in line


def test_failure_and_semantic_colors(sl, monkeypatch):
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr(sl, "_status_prefix", lambda _: sl._fail_glyph("server_error"))
    monkeypatch.setattr(sl, "_credits_segment", lambda: " · \033[32mcredits: $5.00\033[0m")
    monkeypatch.setattr(sl, "_update_segment", lambda: " · \033[33mupdate\033[0m")
    line = sl._tetris_line(SESSION)
    assert "\033[1;31m✕ (server_error)" in line
    assert "\033[32mcredits: $5.00" in line and "\033[33mupdate" in line
    # A failure leads the line with the red verdict, then the wordmark.
    assert plain(sl, line).startswith("✕ (server_error) cognee manual")


def test_render_exception_falls_back(sl, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": SESSION})))

    def broken(_):
        raise ValueError("partial marker")

    monkeypatch.setattr(sl, "_tetris_line", broken)
    expected = sl._classic_line(SESSION)
    sl.main()
    assert capsys.readouterr().out == expected


def run_renderer(suite, home, source, payload, **extra):
    env = build_env(suite, home)
    env.update(COGNEE_PLUGIN_DATASET="manual", COLUMNS="80", PYTHONPATH=str(suite.scripts_dir))
    env.update(extra)
    return subprocess.run(
        [sys.executable, str(source)],
        input=payload.encode(),
        capture_output=True,
        env=env,
        timeout=10,
    )


@pytest.mark.parametrize(
    "payload",
    ["", "garbage", "[]", '{"workspace":42}', '{"session_id":"../outside"}', '{"session_id":"."}'],
)
def test_bad_input_exits_cleanly(sl, suite, temp_home, payload):
    result = run_renderer(suite, temp_home, sl.__file__, payload, COLUMNS="20")
    assert result.returncode == 0 and result.stderr == b""
    assert len(result.stdout.splitlines()) == 1
    assert sl._width(result.stdout.decode()) <= 20


def test_cp1252_stdout_still_renders_utf8(sl, suite, temp_home):
    # Windows pipes default to cp1252; the renderer switches stdout to UTF-8 in
    # both styles (as before 1.6.22), so Windows users keep the real glyphs.
    result = run_renderer(
        suite,
        temp_home,
        sl.__file__,
        json.dumps({"session_id": SESSION}),
        PYTHONIOENCODING="cp1252",
        COLUMNS="120",
    )
    assert result.returncode == 0 and result.stderr == b""
    output = result.stdout.decode("utf-8")
    assert "cognee" in output and "·" in output


def test_ascii_fallback_when_stdout_cannot_be_switched(sl, monkeypatch):
    monkeypatch.setattr(sl, "_unicode_output", lambda: False)
    line = sl._tetris_line(SESSION)
    assert line.isascii() and "cognee" in line


def test_credit_and_update_notices_survive_without_a_reported_width(sl, monkeypatch):
    # Claude Code does not normally export COLUMNS to the status line command.
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(sl, "_credits_segment", lambda: " · credits: $0.40 · top up: x.test")
    monkeypatch.setattr(sl, "_update_segment", lambda: " · ⬆ update available")
    line = sl._tetris_line(SESSION)
    assert "credits: $0.40" in line and "update available" in line
    # A real narrow terminal may lose decorations and counts, never the notices.
    monkeypatch.setenv("COLUMNS", "60")
    narrow = sl._tetris_line(SESSION)
    assert "credits: $0.40" in narrow and "update available" in narrow


@pytest.mark.parametrize("counts", ["true", "full", "false"])
@pytest.mark.parametrize("url", ["http://localhost:8011", "https://memory.example.test"])
def test_classic_byte_identical_to_1_6_21(sl, suite, temp_home, counts, url):
    # Frozen verbatim from cognee-integrations 962c486; do not regenerate on changes.
    baseline = Path(__file__).resolve().parents[2] / "fixtures" / "statusline_1_6_21.py"
    env = {
        "COGNEE_STATUSLINE_STYLE": "classic",
        "COGNEE_STATUSLINE_COUNTS": counts,
        "COGNEE_BASE_URL": url,
    }
    payload = json.dumps({"session_id": SESSION})
    old = run_renderer(suite, temp_home, baseline, payload, **env)
    new = run_renderer(suite, temp_home, sl.__file__, payload, **env)
    assert old.returncode == new.returncode == 0
    assert old.stderr == new.stderr == b""
    assert old.stdout == new.stdout and old.stdout


@pytest.mark.parametrize("corrupt", ["{partial", "[]", '{"entries_injected":"bad"}'])
def test_corrupt_stats_keep_bar(sl, corrupt):
    (sl._SESSIONS_DIR / SESSION / "stats.json").write_text(corrupt)
    assert "cognee" in sl._tetris_line(SESSION)


def test_render_makes_no_writes(sl):
    before = {str(p): p.read_bytes() for p in sl._RECALL_DIR.parent.rglob("*") if p.is_file()}
    sl._tetris_line(SESSION)
    after = {str(p): p.read_bytes() for p in sl._RECALL_DIR.parent.rglob("*") if p.is_file()}
    assert before == after


def test_failure_at_60_keeps_identity_and_count(sl, monkeypatch):
    monkeypatch.setenv("COLUMNS", "60")
    monkeypatch.setattr(sl, "_status_prefix", lambda _: sl._fail_glyph("server_error"))
    line = sl._tetris_line(SESSION)
    assert sl._width(line) <= 60
    assert all(value in line for value in ("server_error", "manual", "cloud"))


def test_home_without_markers(sl, suite, tmp_path):
    fresh = tmp_path / "empty-home"
    write_json(
        fresh / ".claude" / "settings.json", {"enabledPlugins": {"cognee-memory@cognee": True}}
    )
    result = run_renderer(suite, fresh, sl.__file__, "{}")
    assert result.returncode == 0 and result.stderr == b""
    assert "cognee" in result.stdout.decode()
    assert len(result.stdout.splitlines()) == 1
