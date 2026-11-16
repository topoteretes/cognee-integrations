"""Launch hooks the way the hosts do: hooks.json command strings, through the host's shell.

Everything else in the suite runs a hook as ``[sys.executable, script]`` with a
hand-built environment. The hosts do something else, and on Windows that
difference is where users break:

* **Codex** substitutes ``${PLUGIN_ROOT}`` into the command text and runs it as
  ``%COMSPEC% /C "<command>"`` on Windows (``commandWindows`` when present) and
  ``$SHELL -lc "<command>"`` elsewhere (codex-rs/hooks, command_runner.rs and
  discovery.rs). The environment is the session's, plus PLUGIN_ROOT,
  CLAUDE_PLUGIN_ROOT, PLUGIN_DATA and CLAUDE_PLUGIN_DATA.
* **Claude Code** substitutes ``${CLAUDE_PLUGIN_ROOT}`` (forward slashes on
  Windows) and runs the command with bash: Git Bash on Windows, PowerShell only
  when Git Bash is missing (code.claude.com/docs/en/hooks). The environment is
  inherited.

The plugin itself is copied to where the host installs it under the test HOME,
so a profile path with a space or a non-ASCII name reaches the scripts exactly
as it would on a user's machine.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .isolation import scrub_env_dict
from .suites import Suite

RUNNER = "hook_runner.py"

#: The plugin-root placeholder each host substitutes into hook commands.
PLACEHOLDERS = {"codex": "${PLUGIN_ROOT}", "claude-code": "${CLAUDE_PLUGIN_ROOT}"}

#: Where each host keeps an installed plugin, relative to HOME.
_INSTALL_DIRS = {
    "codex": (".codex", "plugins", "cache", "cognee", "cognee"),
    "claude-code": (".claude", "plugins", "cache", "cognee", "cognee-memory"),
}

#: The knobs from isolation.DETERMINISTIC_ENV that keep background processes
#: out of a test, and nothing else: no PYTHONIOENCODING (users don't set it, and
#: it hid the Windows encoding bugs) and no COGNEE_PLUGIN_IN_VENV (a fresh HOME
#: has no venv, and cloud mode never builds one).
QUIET_ENV = {
    "COGNEE_IDLE_DISABLED": "1",
    "COGNEE_UPDATE_CHECK": "off",
    "COGNEE_LAZY_BOOTSTRAP": "0",
    "COGNEE_LLM_OBSERVER": "false",
}


def launches_through_runner(suite: Suite) -> bool:
    return suite.name in PLACEHOLDERS


def plugin_source(suite: Suite) -> Path:
    """The plugin tree in the checkout (the dir the hook commands' root points at)."""
    return suite.scripts_dir.parent


def plugin_version(suite: Suite) -> str:
    return json.loads(suite.plugin_manifest.read_text(encoding="utf-8"))["version"]


def install_plugin(suite: Suite, home: Path) -> Path:
    """Copy the plugin to the host's install location under ``home``; return its root."""
    root = home.joinpath(*_INSTALL_DIRS[suite.name], plugin_version(suite))
    shutil.copytree(
        plugin_source(suite),
        root,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", "tests"),
    )
    return root


def script_pattern(suite: Suite) -> re.Pattern:
    """Matches a quoted ``"<placeholder>/scripts/<name>"`` and captures ``<name>``."""
    return re.compile(r'"' + re.escape(PLACEHOLDERS[suite.name]) + r'/scripts/([^"]+)"')


def hook_entries(suite: Suite, event: str, match: str | None = None) -> list[dict]:
    """The hooks registered for ``event``, in order, filtered by matcher like the host.

    ``match`` is what the matcher is tested against: the SessionStart source or
    the tool name. A missing matcher, ``""`` or ``"*"`` matches everything.
    """
    spec = json.loads(suite.hooks_json.read_text(encoding="utf-8"))
    entries = []
    for group in spec["hooks"].get(event, []):
        matcher = group.get("matcher") or "*"
        if matcher != "*" and match is not None and not re.fullmatch(matcher, match):
            continue
        entries.extend(group["hooks"])
    return entries


def all_hook_entries(suite: Suite) -> list[tuple[str, dict]]:
    spec = json.loads(suite.hooks_json.read_text(encoding="utf-8"))
    return [
        (event, hook)
        for event, groups in spec["hooks"].items()
        for group in groups
        for hook in group["hooks"]
    ]


def git_bash() -> str | None:
    """Git for Windows' bash, the one Claude Code uses (not System32's WSL launcher)."""
    git = shutil.which("git")
    candidates = [Path(git).resolve().parents[1] / "bin" / "bash.exe"] if git else []
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.append(Path(program_files) / "Git" / "bin" / "bash.exe")
    return next((str(c) for c in candidates if c.is_file()), None)


def host_shell_available(suite: Suite) -> bool:
    if suite.name == "claude-code":
        return bool(git_bash() if os.name == "nt" else shutil.which("bash"))
    return True


def host_command(
    suite: Suite, hook: dict, plugin_root: Path, *, login_shell: bool = True
) -> list[str] | str:
    """The process the host spawns for ``hook``.

    ``login_shell=False`` swaps Codex's ``$SHELL -lc`` for ``/bin/sh -c`` on
    POSIX, for tests that pin PATH: a login shell may rebuild PATH from the
    system profile (macOS path_helper does).
    """
    if suite.name == "codex":
        command = hook["command"]
        if os.name == "nt":
            command = hook.get("commandWindows") or command
        command = command.replace(PLACEHOLDERS["codex"], str(plugin_root))
        if os.name == "nt":
            # `/C`, then the command wrapped in one pair of quotes, as a raw arg.
            return '{} /C "{}"'.format(os.environ.get("COMSPEC", "cmd.exe"), command)
        if not login_shell:
            return ["/bin/sh", "-c", command]
        return [os.environ.get("SHELL") or "/bin/sh", "-lc", command]

    command = hook["command"].replace(PLACEHOLDERS["claude-code"], plugin_root.as_posix())
    bash = git_bash() if os.name == "nt" else shutil.which("bash")
    if not bash:
        raise RuntimeError("no bash to run Claude Code hooks with")
    return [bash, "-c", command]


def host_env(
    suite: Suite,
    home: Path,
    plugin_root: Path,
    *,
    extra: dict[str, str] | None = None,
    quiet: bool = True,
) -> dict[str, str]:
    """The environment a host hands a hook: the user's, HOME moved, plus the host's vars."""
    env = scrub_env_dict(dict(os.environ))
    for name in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME"):
        env.pop(name, None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if os.name == "nt":
        drive, rest = os.path.splitdrive(str(home))
        env["HOMEDRIVE"], env["HOMEPATH"] = drive, rest
    if quiet:
        env.update(QUIET_ENV)
    data = home / ".plugin-data" / suite.name
    if suite.name == "codex":
        env.update(
            PLUGIN_ROOT=str(plugin_root),
            CLAUDE_PLUGIN_ROOT=str(plugin_root),
            PLUGIN_DATA=str(data),
            CLAUDE_PLUGIN_DATA=str(data),
        )
    else:
        env.update(CLAUDE_PLUGIN_ROOT=str(plugin_root), CLAUDE_PLUGIN_DATA=str(data))
    if extra:
        env.update(extra)
    return env


@dataclass
class HookRun:
    event: str
    script: str
    returncode: int
    stdout_bytes: bytes
    stderr_bytes: bytes

    @property
    def stdout(self) -> str:
        # Strict: hosts read hook output as UTF-8, so anything else is a bug.
        return self.stdout_bytes.decode("utf-8")

    @property
    def stderr(self) -> str:
        return self.stderr_bytes.decode("utf-8", "replace")

    def json_output(self) -> dict | None:
        """The hook's stdout as JSON, or None when it printed nothing."""
        text = self.stdout.strip()
        return json.loads(text) if text else None


def fire(
    suite: Suite,
    event: str,
    payload: dict,
    *,
    plugin_root: Path,
    env: dict[str, str],
    cwd: Path,
    match: str | None = None,
    timeout: float = 120.0,
    login_shell: bool = True,
) -> list[HookRun]:
    """Run every hook registered for ``event`` the way the host would, in order."""
    runs = []
    stdin = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    pattern = script_pattern(suite)
    for hook in hook_entries(suite, event, match):
        names = [n for n in pattern.findall(hook["command"]) if n != RUNNER]
        proc = subprocess.run(
            host_command(suite, hook, plugin_root, login_shell=login_shell),
            input=stdin,
            env=env,
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
        )
        runs.append(
            HookRun(
                event=event,
                script=names[0] if names else hook["command"],
                returncode=proc.returncode,
                stdout_bytes=proc.stdout,
                stderr_bytes=proc.stderr,
            )
        )
    return runs


# --- interpreter shims ---------------------------------------------------------
#
# A shim is a fake `python3` / `python` / `py` on PATH. A present one records its
# own name in COGNEE_TEST_INTERPRETER and runs the real interpreter; a store stub
# imitates the Microsoft Store alias (prints the Store message and exits 9009
# without running anything); a missing one answers like a shell that found
# nothing, which masks a real interpreter later on PATH. Names without a shim are
# simply absent when PATH holds only the shims.

_STORE_MESSAGE = "Python was not found; run without arguments to install from the Microsoft Store"


def make_shims(
    directory: Path,
    present: tuple[str, ...] = (),
    store_stubs: tuple[str, ...] = (),
    missing: tuple[str, ...] = (),
) -> Path:
    """Write interpreter shims for the shell the host uses; return the dir to put on PATH."""
    directory.mkdir(parents=True, exist_ok=True)
    real = Path(sys.executable)
    for name in present:
        _write_shim(directory, name, real, kind="present")
    for name in store_stubs:
        _write_shim(directory, name, real, kind="store")
    for name in missing:
        _write_shim(directory, name, real, kind="missing")
    return directory


def _write_shim(directory: Path, name: str, real: Path, *, kind: str) -> None:
    # A .cmd for cmd.exe (Codex on Windows) and an extensionless sh script for
    # bash (Claude Code's Git Bash, and every POSIX shell). cmd resolves the
    # .cmd through PATHEXT; bash only runs the extensionless one.
    if kind == "store":
        cmd = f"@echo off\r\necho {_STORE_MESSAGE} 1>&2\r\nexit /b 9009\r\n"
        sh = f"#!/bin/sh\necho '{_STORE_MESSAGE}' >&2\nexit 49\n"
    elif kind == "missing":
        cmd = (
            f"@echo off\r\necho '{name}' is not recognized as an internal or external"
            " command 1>&2\r\nexit /b 9009\r\n"
        )
        sh = f"#!/bin/sh\necho '{name}: command not found' >&2\nexit 127\n"
    else:
        strip = 'if "%~1"=="-3" shift\r\n' if name == "py" else ""
        cmd = (
            "@echo off\r\n"
            f'set "COGNEE_TEST_INTERPRETER={name}"\r\n'
            f"{strip}"
            f'"{real}" %1 %2 %3 %4 %5 %6 %7 %8 %9\r\n'
            "exit /b %errorlevel%\r\n"
        )
        strip_sh = '[ "$1" = "-3" ] && shift\n' if name == "py" else ""
        sh = (
            "#!/bin/sh\n"
            f"COGNEE_TEST_INTERPRETER={name}; export COGNEE_TEST_INTERPRETER\n"
            f"{strip_sh}"
            f'exec "{real.as_posix()}" "$@"\n'
        )
    if os.name == "nt":
        (directory / f"{name}.cmd").write_bytes(cmd.encode("utf-8"))
    target = directory / name
    # Bytes, not write_text: the line endings are part of the shim, and
    # write_text only gained `newline=` in Python 3.10.
    target.write_bytes(sh.encode("utf-8"))
    target.chmod(0o755)


def shim_path_env(shims: Path) -> dict[str, str]:
    """PATH holding only the shims (plus what Windows itself needs to start processes)."""
    if os.name == "nt":
        system = os.environ.get("SystemRoot", r"C:\Windows")
        return {"PATH": os.pathsep.join([str(shims), str(Path(system) / "System32")])}
    return {"PATH": str(shims)}


# --- a whole session -------------------------------------------------------------


def make_home(tmp_path: Path, profile: str) -> Path:
    """A user profile dir, ``<tmp>/Users/<profile>``, with an empty ``~/.cognee``."""
    home = tmp_path / "Users" / profile
    (home / ".cognee").mkdir(parents=True)
    return home


def write_env_file(home: Path, url: str, api_key: str, *, bom: bool = False) -> None:
    """Cloud mode the way the setup instructions configure it: ``~/.cognee/.env``."""
    data = f'COGNEE_BASE_URL="{url}"\nCOGNEE_API_KEY={api_key}\n'.encode("utf-8")
    (home / ".cognee").mkdir(parents=True, exist_ok=True)
    (home / ".cognee" / ".env").write_bytes(b"\xef\xbb\xbf" + data if bom else data)


class HostSession:
    """One host session against a mock server, run hook by hook as the host would.

    The plugin is installed under ``home`` and the environment is the host's
    (see ``host_env``). ``sid`` is unique per session: the exit watcher and the
    deferred SessionEnd sync are detached and can reach a mock server after the
    session that started them is over, so assertions filter on it.
    """

    def __init__(self, suite: Suite, home: Path, project: Path, mock_server, env_extra=None):
        import uuid

        from . import payloads

        self.suite = suite
        self.home = home
        self.project = project
        self.mock = mock_server
        self.payloads = payloads
        self.root = install_plugin(suite, home)
        extra = {"COGNEE_PLATFORM_API_URL": mock_server.url, **(env_extra or {})}
        self.env = host_env(suite, home, self.root, extra=extra)
        self.runs: list[HookRun] = []
        self.sid = f"sess-{uuid.uuid4().hex[:12]}"

    def fire(self, event: str, payload: dict, match: str | None = None) -> list[HookRun]:
        runs = fire(
            self.suite,
            event,
            payload,
            plugin_root=self.root,
            env=self.env,
            cwd=self.project,
            match=match,
        )
        self.runs.extend(runs)
        return runs

    def start(self) -> list[HookRun]:
        payload = self.payloads.session_start(session_id=self.sid, cwd=str(self.project))
        return self.fire("SessionStart", payload, match="startup")

    def prompt(self, text: str) -> list[HookRun]:
        payload = self.payloads.user_prompt(session_id=self.sid, cwd=str(self.project), prompt=text)
        return self.fire("UserPromptSubmit", payload)

    def tool_call(self, tool: str = "Bash") -> list[HookRun]:
        payload = self.payloads.post_tool_use(
            session_id=self.sid, cwd=str(self.project), tool_name=tool
        )
        return self.fire("PostToolUse", payload, match=tool)

    def stop(self, answer: str = "Here is the answer.") -> list[HookRun]:
        payload = self.payloads.stop(
            session_id=self.sid, cwd=str(self.project), assistant_message=answer
        )
        return self.fire("Stop", payload)

    def end(self) -> list[HookRun]:
        payload = self.payloads.session_end(session_id=self.sid, cwd=str(self.project))
        return self.fire("SessionEnd", payload)

    def full(self, first: str, second: str) -> None:
        """start -> prompt -> tool call -> Stop -> second prompt -> SessionEnd."""
        self.start()
        self.prompt(first)
        self.tool_call()
        self.stop()
        self.prompt(second)
        self.end()

    def outputs(self, event: str, script: str) -> list[dict]:
        return [
            r.json_output()
            for r in self.runs
            if r.event == event and r.script == script and r.stdout.strip()
        ]

    def calls(self, path: str) -> list[dict]:
        """JSON bodies of this session's POSTs to ``path``."""
        bodies = [
            c.get("json") or {}
            for c in self.mock.calls
            if c["method"] == "POST" and c["path"] == path
        ]
        return [b for b in bodies if str(b.get("session_id", "")).endswith(self.sid)]

    def assert_every_hook_succeeded(self) -> None:
        failed = [
            (r.event, r.script, r.returncode, r.stderr[-500:]) for r in self.runs if r.returncode
        ]
        assert not failed, f"{self.suite.name}: hooks failed as the host would see them: {failed}"
        crashes = list(self.home.rglob("hook-crash.log"))
        assert not crashes, crashes[0].read_text(encoding="utf-8")
        for r in self.runs:
            r.json_output()  # every stdout is valid UTF-8 and, if present, valid JSON
