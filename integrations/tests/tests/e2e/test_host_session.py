"""A person's session, with every hook launched the way the host launches it.

The rest of e2e/ runs one script at a time as ``[sys.executable, script]`` with
PYTHONIOENCODING pinned and a hand-built environment. That is how every Windows
user of the Codex plugin could see "Hook failed" on every prompt while the whole
suite stayed green. Here the real hook scripts run from a copy of the plugin
installed where the host installs it, through the hooks.json command strings and
the host's own shell (see utils.host_launch), in the order a session produces:

    SessionStart -> prompt -> tool call -> Stop -> second prompt -> SessionEnd

in cloud mode, which is what that user runs, configured the way the setup
instructions configure it: through ``~/.cognee/.env``, not exported variables.

Covered here: the full lifecycle, text people type, profile paths with spaces
and non-ASCII letters, a Windows session with no resolvable home, and the
documented setup snippets, run with the shell they are written for. Interpreter
selection and the host shells on their own are in unit/test_hook_runner.py; the
skills, run the way the agent runs them, in e2e/test_skills_as_the_agent_runs_them.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from utils import host_launch as hl
from utils.fixtures import DEFAULT_TEST_API_KEY
from utils.host_launch import HostSession, make_home, write_env_file

MEMORY = "The project codename is BLUEFIN."


@pytest.fixture
def host(suite):
    if not hl.launches_through_runner(suite):
        pytest.skip(f"{suite.name}: hooks are not launched through hooks.json command strings here")
    if not hl.host_shell_available(suite):
        pytest.skip(f"{suite.name}: the host's shell is not installed")
    return suite


def _context(output: dict) -> str:
    return output["hookSpecificOutput"]["additionalContext"]


# --- the lifecycle, from an ordinary and an awkward profile path ----------------


@pytest.mark.parametrize(
    "profile",
    ["cognee", "Test User", "Tëst Üsér"],
    ids=["plain", "space", "non-ascii"],
)
def test_a_full_session_works_the_way_the_host_runs_it(
    host, private_mock_server, tmp_path, profile
):
    mock = private_mock_server
    home = make_home(tmp_path, profile)
    write_env_file(home, mock.url, DEFAULT_TEST_API_KEY)
    project = home / "projects" / "my app"
    project.mkdir(parents=True)
    mock.set_recall_results([{"source": "graph", "text": MEMORY}])

    session = HostSession(host, home, project, mock)
    first, second = "what is the project codename?", "and what did you just tell me?"
    session.full(first, second)

    session.assert_every_hook_succeeded()

    started = session.outputs("SessionStart", "session-start.py")
    assert started and started[0]["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    # The session is tied to a dataset: the user's `"current": {"name": ""}` symptom.
    registered = session.calls("/api/v1/agents/register")
    assert registered, "SessionStart never registered the session"
    assert registered[-1].get("dataset_ids"), registered[-1]

    # Both prompts recalled, with the prompt as typed, and the memory reached the model.
    recalls = session.calls("/api/v1/recall")
    assert [r["query"] for r in recalls] == [first, second]
    lookups = session.outputs("UserPromptSubmit", "session-context-lookup.py")
    assert len(lookups) == 2 and all(MEMORY in _context(o) for o in lookups), lookups

    # The turn was captured: the tool trace and the prompt/answer pair.
    entries = [c["entry"] for c in session.calls("/api/v1/remember/entry")]
    assert any(e.get("type") == "trace" and e.get("origin_function") == "Bash" for e in entries)
    assert any(e.get("type") == "qa" and e.get("question") == first for e in entries), entries


# --- text people type ---------------------------------------------------------


def test_text_outside_the_ansi_code_page_survives_the_round_trip(
    host, private_mock_server, tmp_path
):
    """Windows pipes default to cp1252, which has none of these characters."""
    mock = private_mock_server
    home = make_home(tmp_path, "cognee")
    write_env_file(home, mock.url, DEFAULT_TEST_API_KEY)
    project = home / "project"
    project.mkdir()
    remembered = "Кодовое имя — 蓝鳍 ✓ 🐟"
    mock.set_recall_results([{"source": "graph", "text": remembered}])

    session = HostSession(host, home, project, mock)
    prompt = "what's the codename? 记忆 ✓ 🚀 naïve café"
    session.start()
    session.prompt(prompt)
    session.stop(answer="蓝鳍 🐟")

    session.assert_every_hook_succeeded()
    assert [r["query"] for r in session.calls("/api/v1/recall")] == [prompt]
    [lookup] = session.outputs("UserPromptSubmit", "session-context-lookup.py")
    assert remembered in _context(lookup)
    entries = [c["entry"] for c in session.calls("/api/v1/remember/entry")]
    assert any(e.get("question") == prompt and e.get("answer") == "蓝鳍 🐟" for e in entries), (
        entries
    )


# --- the environment a host passes --------------------------------------------


@pytest.mark.skipif(
    os.name != "nt", reason="POSIX resolves a missing HOME from the passwd database"
)
def test_a_session_without_a_resolvable_home_is_reported_not_silent(
    host, private_mock_server, tmp_path
):
    """Codex rebuilds the hook environment; if the profile variables don't survive, say so.

    With USERPROFILE, HOMEDRIVE/HOMEPATH and HOME all gone, Python cannot find a
    home directory. The hooks must not exit 1 with nothing to show for it: every
    hook exits 0, and the user gets a message from Cognee.
    """
    home = make_home(tmp_path, "cognee")
    project = home / "project"
    project.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    session = HostSession(
        host, home, project, private_mock_server, env_extra={"TEMP": str(temp), "TMP": str(temp)}
    )
    for name in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME"):
        session.env.pop(name, None)

    session.start()
    session.prompt("what is the project codename?")

    failed = [
        (r.event, r.script, r.returncode, r.stderr[-300:]) for r in session.runs if r.returncode
    ]
    assert not failed, failed

    def message(run) -> str:
        out = run.json_output() or {}
        return out.get("systemMessage") or out.get("hookSpecificOutput", {}).get(
            "systemMessage", ""
        )

    assert any("Cognee" in message(r) for r in session.runs), (
        f"the user was told nothing: {session.runs}"
    )


# --- the setup snippets people paste ---------------------------------------

_README_BASH = """mkdir -p ~/.cognee
cat >> ~/.cognee/.env <<'EOF'
COGNEE_BASE_URL="{url}"
COGNEE_API_KEY="{key}"
EOF
chmod 600 ~/.cognee/.env
"""

_README_POWERSHELL = """New-Item -ItemType Directory -Force "$env:USERPROFILE\\.cognee" | Out-Null
@'
COGNEE_BASE_URL="{url}"
COGNEE_API_KEY="{key}"
'@ | Add-Content "$env:USERPROFILE\\.cognee\\.env"
"""

# What the Codex user in the Windows report pasted (the cloud dashboard's
# snippet): Set-Content in Windows PowerShell's default encoding, then an ACL
# that removes inheritance and grants only the user read/write/delete.
_DASHBOARD_POWERSHELL = (
    'New-Item -ItemType Directory -Force "$env:USERPROFILE\\.cognee" | Out-Null; '
    '$f = "$env:USERPROFILE\\.cognee\\.env"; $t = "$f.new"; '
    "@(if (Test-Path $f) {{ Get-Content $f | Where-Object {{ $_ -cnotmatch "
    "'^\\s*(export\\s+)?(COGNEE_BASE_URL|COGNEE_API_KEY)=' }} }}) + "
    "@('COGNEE_BASE_URL=\"{url}\"', 'COGNEE_API_KEY={key}') | Set-Content $t; "
    'if ($?) {{ icacls $t /inheritance:r /grant:r "$($env:USERNAME):(R,W,D)" | Out-Null; '
    "Move-Item -Force $t $f }}"
)


def _run_setup(method: str, home: Path, url: str) -> None:
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    if method == "bom":
        # What Windows PowerShell 5.1 writes for `Out-File -Encoding utf8`.
        write_env_file(home, url, DEFAULT_TEST_API_KEY, bom=True)
        return
    if method == "readme-bash":
        script = _README_BASH.format(url=url, key=DEFAULT_TEST_API_KEY)
        subprocess.run(["bash", "-c", script], env=env, check=True, timeout=60)
        return
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is not installed")
    template = _README_POWERSHELL if method == "readme-powershell" else _DASHBOARD_POWERSHELL
    script = template.format(url=url, key=DEFAULT_TEST_API_KEY)
    subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        check=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    "method",
    [
        pytest.param(
            "readme-bash",
            marks=pytest.mark.skipif(os.name == "nt", reason="the bash snippet is for macOS/Linux"),
        ),
        pytest.param(
            "readme-powershell",
            marks=pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell only"),
        ),
        pytest.param(
            "dashboard-powershell",
            marks=pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell only"),
        ),
        "bom",
    ],
)
def test_each_setup_snippet_gives_a_working_first_prompt(
    host, private_mock_server, tmp_path, method
):
    mock = private_mock_server
    home = tmp_path / "Users" / "Test User"
    home.mkdir(parents=True)
    _run_setup(method, home, mock.url)
    assert (home / ".cognee" / ".env").is_file()
    project = home / "project"
    project.mkdir()
    mock.set_recall_results([{"source": "graph", "text": MEMORY}])

    session = HostSession(host, home, project, mock)
    session.start()
    session.prompt("what is the project codename?")

    session.assert_every_hook_succeeded()
    # The key from the file reached the server, and the prompt recalled memory.
    assert any(c["headers"].get("X-Api-Key") == DEFAULT_TEST_API_KEY for c in mock.calls), (
        "the API key in ~/.cognee/.env was never used"
    )
    [lookup] = session.outputs("UserPromptSubmit", "session-context-lookup.py")
    assert MEMORY in _context(lookup)
