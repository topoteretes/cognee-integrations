"""Each setting is read by its own name (SDK-1411, Claude Code).

The plugin directory treats an environment variable whose name is only known
at run time as a possible credential read. The scripts therefore spell every
read out, and helpers take the value, not the variable's name. The one place
that cannot is the settings-file loader, which copies whatever names the file
holds.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[3] / "claude-code" / "scripts"
#: A read whose first argument is a name, not a string literal.
_RUNTIME_READ = re.compile(
    r"(?:os\.environ\.get|os\.getenv)\(\s*[A-Za-z_][A-Za-z0-9_.]*\s*[,)]"
    r"|os\.environ\.pop\(\s*[A-Za-z_][A-Za-z0-9_]*\s*[,)]"
)
#: The settings-file loader: it mirrors the file's own names into the environment.
_LOADER = "_env_file.py"


def test_no_script_reads_a_variable_by_a_run_time_name():
    offenders = []
    for path in sorted(_SCRIPTS.glob("*.py")):
        if path.name == _LOADER:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _RUNTIME_READ.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()[:90]}")
    assert offenders == []


def test_config_spells_out_every_mapped_setting():
    """``_env_settings`` lists the same names as ``_ENV_MAP``, in the same order."""
    tree = ast.parse((_SCRIPTS / "config.py").read_text(encoding="utf-8"))
    mapped, spelled = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_ENV_MAP" for t in node.targets
        ):
            mapped = [key.value for key in node.value.keys]
        if isinstance(node, ast.FunctionDef) and node.name == "_env_settings":
            returned = next(n for n in ast.walk(node) if isinstance(n, ast.Return))
            for key, value in zip(returned.value.keys, returned.value.values):
                assert value.args[0].value == key.value, key.value
                spelled.append(key.value)
    assert mapped and spelled == mapped
