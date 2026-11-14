"""Static checks on what the Claude Code plugin ships (SDK-1411).

The plugin reads its keys only from Claude Code's plugin options, which reach
hook processes only. The text the model reads (agents, skills) must not send it
to other credential sources, and no shipped script may hand a request to a
``cognee-cli`` that loads ``.env`` files and calls LLM providers itself.

These are about the Claude Code tree only, so they use its descriptor directly
instead of the ``suite`` fixture.
"""

from __future__ import annotations

import re

from utils.suites import CLAUDE

PLUGIN = CLAUDE.scripts_dir.parent


def _model_facing_markdown():
    """The markdown the model reads and acts on: agents and skills."""
    files = sorted(PLUGIN.glob("agents/*.md")) + sorted(PLUGIN.glob("skills/*/SKILL.md"))
    assert files, "no agent or skill markdown found"
    return files


def _logical_lines(text: str) -> list[str]:
    """Lines with shell ``\\`` continuations joined, so a command reads as one."""
    return re.sub(r"\\\n", " ", text).splitlines()


def _shipped_scripts():
    return sorted(
        p
        for p in PLUGIN.rglob("*")
        if p.is_file() and p.suffix in (".py", ".sh") and "__pycache__" not in p.parts
    )


def test_api_key_variable_only_after_the_forget_helper_resolves_it():
    """``${COGNEE_API_KEY`` is empty in a shell unless ``cognee-forget.sh env``
    set it in the same invocation; anywhere else it is an unauthenticated call."""
    resolver = re.compile(r'eval "\$\(.*cognee-forget\.sh"? env\)"')
    offenders = []
    for path in _model_facing_markdown():
        for line in _logical_lines(path.read_text(encoding="utf-8")):
            if "${COGNEE_API_KEY" in line and not resolver.search(line):
                offenders.append(f"{path.relative_to(PLUGIN)}: {line.strip()[:120]}")
    assert offenders == []


def test_no_file_or_env_file_is_named_as_a_key_source():
    """No key is stored on disk, and one in ``~/.cognee/.env`` is ignored."""
    offenders = []
    for path in _model_facing_markdown():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "api_key.json" in line:
                offenders.append(f"{path.relative_to(PLUGIN)}: {line.strip()[:120]}")
            elif "~/.cognee/.env" in line and re.search(r"\bkey\b|API_KEY", line, re.I):
                offenders.append(f"{path.relative_to(PLUGIN)}: {line.strip()[:120]}")
    assert offenders == []


def test_model_facing_text_has_no_cognee_cli_fallback():
    offenders = [
        str(path.relative_to(PLUGIN))
        for path in _model_facing_markdown()
        if "cognee-cli" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# A run of the CLI: an argv list that starts with it (Python), or a shell line
# whose command is it (optionally behind exec / uv run / uvx).
_PY_RUN = re.compile(r"""\[\s*["']cognee-cli["']""")
_SH_RUN = re.compile(r"^\s*(?:exec\s+|uv\s+run\s+|uvx\s+)?cognee-cli\b", re.M)


def test_no_shipped_script_runs_cognee_cli():
    offenders = []
    for path in _shipped_scripts():
        text = path.read_text(encoding="utf-8", errors="replace")
        pattern = _PY_RUN if path.suffix == ".py" else _SH_RUN
        if pattern.search(text):
            offenders.append(str(path.relative_to(PLUGIN)))
    assert offenders == []
    # The detector itself: both shapes the removed fallback had are caught.
    assert _PY_RUN.search('subprocess.run(["cognee-cli", "recall", q])')
    assert _SH_RUN.search('if x; then\n    cognee-cli recall "$q" -f json\nfi')
