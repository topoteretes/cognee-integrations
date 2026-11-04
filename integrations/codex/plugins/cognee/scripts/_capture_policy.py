"""Client-side automatic capture controls. Explicit remember is independent."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
from pathlib import PurePosixPath

_FALSE = {"0", "false", "no", "off"}
_PATH_KEYS = {"file_path", "filepath", "path", "paths", "notebook_path", "filename"}
_DENY_PATHS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    ".npmrc",
    ".netrc",
    "*/.aws/credentials",
    "*/.ssh/*",
    "*.p12",
    "*.pfx",
    "secrets.*",
    "credentials.*",
)
# The last word of a secret-bearing name. ``secret`` may carry a ``_key`` /
# ``_access_key`` suffix (``SECRET_KEY``, ``aws_secret_access_key``), and the
# AWS / npm names a plain suffix test misses are spelled out: ``access_key_id``,
# camelCase ``SessionToken`` / ``SecretAccessKey`` / ``_authToken``.
_SECRET_WORD = (
    r"secret(?:[_-]?(?:access[_-]?)?key)?|token|password|passwd|api[_-]?key"
    r"|access[_-]?key(?:[_-]?id)?|session[_-]?token|auth[_-]?token"
)
_SECRET_KEY = re.compile(rf"(?i)^(?:authorization|x-api-key|_auth|(?:.*[_-])?(?:{_SECRET_WORD}))$")
_RULES = (
    (
        "private-key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    ),
    ("connection", r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^\s\"'<>]+"),
    ("authorization", r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"),
    (
        "credential",
        rf"(?i)[\"']?\b(?:(?:[\w-]*[_-])?(?:{_SECRET_WORD}|x-api-key)|_auth)"
        r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    ),
    (
        "vendor-key",
        r"\b(?:sk-(?:proj-|ant-)?|gh[pousr]_|github_pat_|xox[baprs]-|whsec_)[A-Za-z0-9_-]{16,}"
        r"|\bnpm_[A-Za-z0-9]{36}\b|\bpypi-[A-Za-z0-9_-]{50,}",
    ),
    (
        "aws-key-id",
        r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|APKA|ABIA|ACCA)[A-Z0-9]{16}\b",
    ),
    ("bcrypt", r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}"),
)
_COMPILED = [(kind, re.compile(pattern)) for kind, pattern in _RULES]
# ``Tool(prefix:*)`` borrows Claude Code's permission spelling, so the pattern a
# user already writes to allow a command also selects whether it is remembered.
_COMMAND_PATTERN = re.compile(r"^(?P<tool>[^()\s]+)\((?P<prefix>[^()]*):\*\)$")


class CapturePatternError(ValueError):
    """A ``COGNEE_CAPTURE_TOOLS`` / ``COGNEE_CAPTURE_DENY_TOOLS`` entry is not understood."""


def capture_enabled() -> bool:
    return os.environ.get("COGNEE_CAPTURE", "true").strip().lower() not in _FALSE


def _list_env(name: str, *, separator: str = ",") -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        values = json.loads(raw)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(f"{name} must be an array of strings")
        return values
    return [part.strip() for part in raw.split(separator) if part.strip()]


def _glob_match(name: str, pattern: str) -> bool:
    """``fnmatch.fnmatchcase``, with an unusable glob reported as a pattern error.

    Python 3.9's fnmatch hands some globs (a reversed range such as ``[C-A]``) to
    ``re`` unchanged, which rejects them; 3.10+ turn them into "matches nothing".
    Without this the 3.9 ``re.error`` escaped the hook as a crash instead of the
    usual "pattern not understood" report.
    """
    try:
        return fnmatch.fnmatchcase(name, pattern)
    except re.error as exc:
        raise CapturePatternError(
            f"capture pattern {pattern!r} is not a valid glob: {exc}"
        ) from exc


def _sensitive_path(value) -> bool:
    if isinstance(value, (tuple, list)):
        return any(_sensitive_path(item) for item in value)
    if not isinstance(value, str):
        return False
    normalized = value.replace("\\", "/").lower()
    name = PurePosixPath(normalized).name
    return any(
        _glob_match(name, pattern.lower())
        or _glob_match("/" + normalized.lstrip("/"), pattern.lower())
        for pattern in (*_DENY_PATHS, *_list_env("COGNEE_CAPTURE_DENY_PATHS"))
    )


# Shell control characters that end a word for the secret-file check:
# ``cat .env|grep``, ``cat<.env``, ``$(cat .env)``, ``a;cat .env``.
_SHELL_SPLIT = re.compile(r"[;|&()<>`]")
_HOME_VAR = re.compile(r"^\$(?:HOME|\{HOME\})(?=/|\Z)")


def _shell_words(command: str) -> list[str]:
    """Split a shell command into words, POSIX-quoted; whitespace if it will not parse."""
    try:
        words = shlex.split(command)
    except ValueError:  # unbalanced quote, trailing backslash
        words = command.split()
    pieces = []
    for word in words:
        for piece in _SHELL_SPLIT.split(word):
            if piece:
                pieces.append(_HOME_VAR.sub("~", piece))
    return pieces


def _command_reads_secret(params) -> bool:
    """Whether a shell command names a deny-listed path as one of its words.

    A word-level check, not a shell interpreter: ``cat ~/.aws/credentials``,
    ``less "$HOME/.ssh/id_rsa"`` and ``source .env.local`` are caught, but a
    path built at run time (``cat $AWS_DIR/credentials``, ``cat ~/.aws/cred*``)
    is not, and neither is a file read by a program without naming it.
    """
    command = params.get("command") if isinstance(params, dict) else None
    if not isinstance(command, str):
        return False
    return any(_sensitive_path(word) for word in _shell_words(command))


def _has_sensitive_path(value) -> bool:
    if isinstance(value, dict):
        return any(
            (str(key).lower() in _PATH_KEYS and _sensitive_path(item)) or _has_sensitive_path(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_sensitive_path(item) for item in value)
    return False


def _parse_pattern(pattern: str) -> tuple[str, str | None]:
    """Split ``Tool(prefix:*)`` into (tool glob, command prefix); plain names get None."""
    if "(" not in pattern and ")" not in pattern:
        return pattern, None
    match = _COMMAND_PATTERN.match(pattern)
    prefix = match.group("prefix").strip() if match else ""
    if not prefix:
        # Matching nothing is how ``Bash(rg:*)`` used to switch capture off for
        # every tool without a word of warning; a typo has to be loud.
        raise CapturePatternError(
            f"capture tool pattern {pattern!r} is not understood; "
            "use a tool name, an fnmatch glob, or Tool(command-prefix:*)"
        )
    return match.group("tool"), prefix


def _tool_patterns(env_name: str, default: list[str]) -> list[tuple[str, str | None]]:
    """Parse every entry up front so a bad one fails whether or not it is reached."""
    try:
        entries = _list_env(env_name, separator="|") or default
    except ValueError as exc:  # a JSON array that does not parse, or holds non-strings
        raise CapturePatternError(f"{env_name} is not a valid JSON array: {exc}") from exc
    try:
        return [_parse_pattern(pattern) for pattern in entries]
    except CapturePatternError as exc:
        raise CapturePatternError(f"{env_name}: {exc}") from exc


def _matches(tool: str, prefix: str | None, name: str, params) -> bool:
    if not _glob_match(name, tool):
        return False
    if prefix is None:
        return True
    command = params.get("command") if isinstance(params, dict) else None
    if not isinstance(command, str):
        return False
    # The prefix has to end on a word boundary: ``Bash(git:*)`` keeps ``git status``
    # and drops ``gitx weird``. Compound commands (``cd x && git status``) are
    # matched on their first word only; this is a prefix test, not a shell parser.
    command = command.lstrip()
    if not command.startswith(prefix):
        return False
    return len(command) == len(prefix) or command[len(prefix)].isspace()


def allow_tool(name: str, params) -> bool:
    """Whether a tool call may be captured.

    Raises :class:`CapturePatternError` when an allow or deny entry cannot be
    parsed, so a misspelled pattern never silently disables capture.
    """
    if not capture_enabled():
        return False
    allowed = _tool_patterns("COGNEE_CAPTURE_TOOLS", ["*"])
    denied = _tool_patterns("COGNEE_CAPTURE_DENY_TOOLS", [])
    if not any(_matches(tool, prefix, name, params) for tool, prefix in allowed):
        return False
    if any(_matches(tool, prefix, name, params) for tool, prefix in denied):
        return False
    return not (_has_sensitive_path(params) or _command_reads_secret(params))


def capture_pattern_error() -> str:
    """The message for a malformed capture tool pattern in the environment, or ''.

    Cheap enough for a per-prompt hook; lets a hook whose output the user sees
    report a problem that only surfaces inside the silent PostToolUse path.
    """
    try:
        _tool_patterns("COGNEE_CAPTURE_TOOLS", ["*"])
        _tool_patterns("COGNEE_CAPTURE_DENY_TOOLS", [])
    except CapturePatternError as exc:
        return str(exc)
    return ""


def redact(value):
    """Redact before truncation so a clipped private key cannot escape matching."""
    if os.environ.get("COGNEE_CAPTURE_REDACT", "true").strip().lower() in _FALSE:
        return value
    if isinstance(value, str):
        for kind, pattern in _COMPILED:
            value = pattern.sub(f"[redacted:{kind}]", value)
        # User-supplied regexes are JSON strings; commas are valid regex syntax.
        raw = os.environ.get("COGNEE_CAPTURE_REDACT_PATTERNS", "").strip()
        if raw:
            patterns = _list_env("COGNEE_CAPTURE_REDACT_PATTERNS", separator="\n")
            for pattern in patterns:
                value = re.sub(pattern, "[redacted:custom]", value)
        return value
    if isinstance(value, dict):
        return {
            key: "[redacted:credential]" if _SECRET_KEY.match(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


# Only command positions count: grep patterns, Python source and ordinary repo
# paths mentioning Cognee are evidence, not recursive plugin invocations.
_PLUGIN_SCRIPTS = set(
    "cognee-search.sh cognee-remember.sh cognee-doctor.sh "
    "cognee-index-repo.sh cognee-forget.sh cognee-statusline.sh switch-dataset.py "
    "list-datasets.py plugin_eval.py cognee-recap.py hook_runner.py cognee-evidence.py "
    "cognee-cli.sh sync-session-to-graph.py credits-refresh.py".split()
)
# Plugin roots of every host: generic script names such as ``doctor.py`` count
# only when they run from one of these.
_PLUGIN_PATH_MARKERS = (
    "$CLAUDE_PLUGIN_ROOT/",
    "${CLAUDE_PLUGIN_ROOT}/",
    "/.claude/plugins/cache/cognee/",
    "/.cognee-plugin/",
    "$CODEX_PLUGIN_ROOT/",
    "${CODEX_PLUGIN_ROOT}/",
    "/.codex/plugins/cognee/",
    "$CURSOR_PLUGIN_ROOT/",
    "${CURSOR_PLUGIN_ROOT}/",
    "/.cursor/plugins/local/cognee-memory/",
    "COGNEE_ANTIGRAVITY_PLUGIN_ROOT",
    "/.gemini/config/plugins/cognee",
)
_READ_COMMANDS = set(
    "ls cat head tail grep rg sed awk echo find wc sort uniq cut tr jq "
    "diff stat which pwd tree date env du df".split()
)
_GIT_READ = set(
    "log status diff show fetch ls-tree ls-files rev-parse blame grep describe "
    "cat-file merge-base".split()
)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")


def _shell_parts(command: str) -> list[list[str]]:
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lex.whitespace_split = True
        parts = [[]]
        for token in lex:
            if token in {"&&", ";", "||", "|", "&"}:
                parts.append([])
            else:
                parts[-1].append(token)
        return [part for part in parts if part]
    except ValueError:
        return []


def _command_words(words: list[str]) -> list[str]:
    words = list(words)
    while words and _ASSIGNMENT.match(words[0]):
        words.pop(0)
    if words and words[0] in {"command", "exec", "env"}:
        words.pop(0)
        while words and _ASSIGNMENT.match(words[0]):
            words.pop(0)
    return words


def is_plugin_invocation(command: str) -> str:
    """Return a bounded reason for an actual plugin invocation, otherwise ''."""
    for part in _shell_parts(command):
        words = _command_words(part)
        if not words:
            continue
        executable = words[0]
        base = PurePosixPath(executable).name
        if base in {"python", "python3", "bash", "sh", "zsh"}:
            # -c source is deliberately opaque; never classify its string literals.
            if len(words) < 2 or words[1].startswith("-"):
                continue
            executable = words[1]
            base = PurePosixPath(executable).name
        elif base in {"uv", "uvx"}:
            rest = words[1:]
            if rest and rest[0] in {"run", "tool"}:
                rest = rest[1:]
            if rest and rest[0] == "run":
                rest = rest[1:]
            if not rest or rest[0].startswith("-"):
                continue
            executable = rest[0]
            base = PurePosixPath(executable).name
        if base in _PLUGIN_SCRIPTS:
            return "plugin_script"
        if base == "cognee-cli":
            return "cognee_cli"
        if any(marker in executable for marker in _PLUGIN_PATH_MARKERS):
            return "plugin_path"
    return ""


def _inspection_words(command: str) -> list[str]:
    # Ambiguous shell syntax is kept. In particular, never hide writes behind a
    # read command, command substitution, a heredoc, or a pipeline.
    if any(marker in command for marker in (";", "|", "<", ">", "`", "$", "\n", "(")):
        return []
    if "&" in command.replace("&&", ""):
        return []
    parts = _shell_parts(command)
    while len(parts) > 1:
        first = parts.pop(0)
        if first[0] not in {"cd", "export"} and not all(_ASSIGNMENT.match(w) for w in first):
            return []
    return _command_words(parts[0]) if parts else []


def safe_first_token(command: str) -> str:
    """Log a vocabulary member only, never user text or a path basename."""
    parts = _shell_parts(command)
    words = _inspection_words(command) or (_command_words(parts[0]) if parts else [])
    first = PurePosixPath(words[0]).name if words else ""
    known = (
        _READ_COMMANDS
        | _PLUGIN_SCRIPTS
        | {
            "git",
            "gh",
            "cognee-cli",
            "python",
            "python3",
            "bash",
            "sh",
            "zsh",
            "uv",
            "uvx",
            "cd",
            "export",
        }
    )
    return first if first in known else "other"


def low_signal_bash(command: str, output, status: str) -> bool:
    """Conservative inspection filter. Failures, mutations and unknowns are kept."""
    words = _inspection_words(command)
    if not words or not isinstance(output, dict):
        return False  # Without a structured outcome we cannot prove success.
    if (
        output.get("stderr")
        or output.get("interrupted")
        or output.get("is_error")
        or output.get("error")
    ):
        return False
    code = output.get("exit_code", output.get("exitCode", 0))
    first, args = PurePosixPath(words[0]).name, words[1:]
    if code not in (0, "0"):
        if not (code in (1, "1") and first in {"grep", "rg"}):
            return False
    elif status != "success":
        return False
    if first == "git":
        # Global options and aliases are retained rather than guessed at.
        if not args:
            return False
        sub, opts = args[0], args[1:]
        if any(a == "--output" or a.startswith("--output=") for a in opts):
            return False
        if sub in {"branch", "tag"}:
            return not opts or all(
                a in {"--list", "-l", "--show-current", "-a", "-r", "-v", "-vv"} for a in opts
            )
        if sub == "worktree":
            return bool(opts) and opts[0] == "list"
        if sub == "remote":
            return not opts or opts in [["-v"], ["--verbose"]]
        return sub in _GIT_READ
    if first == "gh":
        if args[:2] in [["pr", "view"], ["pr", "checks"], ["run", "view"], ["issue", "view"]]:
            return True
        if args[:1] == ["api"]:
            return not any(
                a in {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
                or a.startswith(("--method=", "--field=", "--raw-field=", "-X", "-f", "-F"))
                for a in args[1:]
            )
        return False
    if first not in _READ_COMMANDS:
        return False
    # Commands which can execute programs or write files require extra care.
    if first == "sed":
        rest = args[1:] if args[:1] == ["-n"] else args
        return (
            bool(rest)
            and bool(re.fullmatch(r"(?:[0-9]+(?:,[0-9]+)?)?[pqd]|s/[^/]+/[^/]+/[gip]*", rest[0]))
            and not any(a.startswith("-") for a in rest[1:])
        )
    if first == "awk":
        return (
            bool(args)
            and bool(re.fullmatch(r"\{\s*print\s*\}", args[0]))
            and not any(a.startswith("-") for a in args[1:])
        )
    if first == "uniq":
        return len(args) <= 1
    if first == "find" and any(
        a
        in {
            "-exec",
            "-execdir",
            "-ok",
            "-okdir",
            "-delete",
            "-fprint",
            "-fprintf",
            "-fls",
            "-fprint0",
        }
        for a in args
    ):
        return False
    if first == "sort" and any(a == "--output" or a.startswith(("-o", "--output=")) for a in args):
        return False
    if first == "date" and any(
        a in {"-s", "--set"} or a.startswith("--set=") or not a.startswith(("-", "+")) for a in args
    ):
        return False
    if first == "env":
        return not args
    return True
