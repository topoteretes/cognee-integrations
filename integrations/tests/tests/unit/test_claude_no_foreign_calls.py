"""Claude Code plugin: no LLM provider code and no second host (SDK-1411).

The directory policy for the Claude Code plugin: the plugin talks only to the
Cognee server it is configured for, and reads no credential of its own.

* No shipped script imports ``litellm`` or ``cognee.*``. Importing cognee runs
  its ``.env`` loader in the plugin's process (reading a project or ``~/.env``
  full of secrets), and a ``litellm`` call goes straight to the user's LLM
  provider. The idle watcher's LLM-key check did both; it now only reads
  whether an LLM key or provider option is set.
* No shipped script references the cloud billing route or the production
  platform host: the credits fetch sent the Cloud key to
  ``api.aws.cognee.ai`` for any remote URL that was not a tenant host.

The Cognee server runs in its own venv (``~/.cognee-plugin/venv``) and is not
plugin code. The plugin runs Python inside that venv only through ``-c``
probes in ``session-start.py`` and ``doctor.py``, which read
``importlib.metadata`` and import nothing from cognee; the string check below
covers them, so no script needs to be excluded.

Claude Code only: the other hosts still ship both.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[3] / "claude-code"
_SKIP_DIRS = {"node_modules", "__pycache__", ".venv"}
_CODE_SUFFIXES = {".py", ".sh", ".json", ".js", ".ts", ".mjs", ".cjs"}
#: Shipped scripts executed by the server's venv interpreter rather than the
#: plugin's own Python. Empty: see the module docstring.
_RUNS_IN_SERVER_VENV: frozenset = frozenset()
_FORBIDDEN_ROOTS = ("cognee", "litellm")


def _shipped_files():
    for path in sorted(_PLUGIN.rglob("*")):
        if not path.is_file() or _SKIP_DIRS & set(path.relative_to(_PLUGIN).parts):
            continue
        yield path


def _is_python(path: Path) -> bool:
    if path.suffix == ".py":
        return True
    if path.suffix:
        return False
    try:
        return path.read_bytes()[:64].startswith(b"#!") and b"python" in path.read_bytes()[:64]
    except OSError:
        return False


def _python_files():
    return [p for p in _shipped_files() if _is_python(p)]


def _code_files():
    return [p for p in _shipped_files() if p.suffix in _CODE_SUFFIXES or _is_python(p)]


def _forbidden(module: str) -> bool:
    return any(module == root or module.startswith(root + ".") for root in _FORBIDDEN_ROOTS)


_CODE_IMPORT = re.compile(r"(?m)^\s*(?:import|from)\s+(cognee|litellm)\b(?!_)")


def _docstring_nodes(tree: ast.AST) -> set:
    nodes = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list):
            for stmt in body:
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                    nodes.add(id(stmt.value))
    return nodes


def _offending_imports(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if _forbidden(alias.name)]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if _forbidden(node.module):
                found.append(node.module)
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if _forbidden(arg.value):
                        found.append(arg.value)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            # Code handed to another interpreter (``python -c "..."``).
            found += [f"string:{m.group(1)}" for m in _CODE_IMPORT.finditer(node.value)]
    return found


def test_the_python_scan_finds_the_scripts():
    names = {p.name for p in _python_files()}
    assert {"idle-watcher.py", "_cloud_worker.py", "session-start.py", "cognee-plugin"} <= names


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(_PLUGIN)))
def test_no_shipped_script_imports_litellm_or_cognee(path):
    if path.name in _RUNS_IN_SERVER_VENV:
        pytest.skip(f"{path.name} runs inside the server venv")
    assert _offending_imports(path) == []


def test_the_import_check_catches_each_form(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""import cognee in a docstring is prose."""\n'
        "import litellm\n"
        "from cognee.infrastructure.llm.config import get_llm_config\n"
        "import cognee_plugin\n"
        "importlib.import_module('cognee.shared')\n"
        "probe = 'import cognee; print(cognee.__version__)'\n",
        encoding="utf-8",
    )
    assert _offending_imports(sample) == [
        "litellm",
        "cognee.infrastructure.llm.config",
        "cognee.shared",
        "string:cognee",
    ]


_FORBIDDEN_TEXT = (
    "billing/credits",
    "api.aws.cognee.ai",
    "COGNEE_PLATFORM_API_URL",
    "COGNEE_STATUSLINE_CREDITS",
    "COGNEE_BILLING_URL",
    "refresh_credits",
    "COGNEE_LLM_KEY_CHECK",
    "COGNEE_LLM_CHECK_INTERVAL",
)


def test_no_shipped_script_reaches_billing_or_the_platform_host():
    offenders = []
    for path in _code_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in _FORBIDDEN_TEXT:
            if needle in text:
                offenders.append(f"{path.relative_to(_PLUGIN)}: {needle}")
    assert offenders == []


def test_credits_scripts_and_hooks_are_gone():
    scripts = _PLUGIN / "scripts"
    assert not (scripts / "_credits.py").exists()
    assert not (scripts / "credits-refresh.py").exists()
    hooks = json.loads((_PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    # StopFailure existed only for the credits refresh.
    assert "StopFailure" not in hooks
    commands = [
        hook.get("command", "")
        for groups in hooks.values()
        for group in groups
        for hook in group.get("hooks", [])
    ]
    assert commands and not any("credits" in command for command in commands)
