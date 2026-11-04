"""Routing contract for the Claude Code plugin's skills.

Claude Code lists each plugin skill to the model as ``cognee-memory:<name>`` with
its ``description`` and ``when_to_use`` joined and cut at 1,536 characters. That
text is all the model has when it decides whether a request in plain words
belongs to a skill, and a frontmatter block that does not parse loads the skill
with no fields at all. These checks keep the frontmatter parseable, bounded,
free of bare generic triggers and explicit about which neighbouring skill an
overlapping request belongs to. They also keep the routing evals under
``claude-code/evals/`` in step with the skill list, and pin the recall hook's
dataset hint, whose bare "Do not switch datasets." beat users who asked for a
switch.

Claude Code only: the Codex and Antigravity skills live in their own trees.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pytest
from utils.recall import load_lookup

PLUGIN = Path(__file__).resolve().parents[3] / "claude-code"
SKILLS = sorted(path.name for path in (PLUGIN / "skills").iterdir() if path.is_dir())
EVALS = PLUGIN / "evals"

#: Claude Code cuts description + when_to_use at 1,536 characters. The listing
#: is sent on every session, so the limits below stay far under that cap.
LISTING_CAP = 1536
DESCRIPTION_LIMIT = 220
LISTING_LIMIT = 600

#: Skills whose requests look alike. Each member must send the others' requests
#: away by name, in a parenthesised redirect such as "(<skill>)" after its
#: "Not for".
OVERLAP_GROUPS = {
    "search-or-switch": ("cognee-search", "cognee-switch-datasets"),
    "memory-writers": ("cognee-remember", "cognee-handoff", "cognee-sync"),
    "evaluation-or-recaps": ("cognee-report", "cognee-standup", "cognee-digest", "cognee-timeline"),
    "code-graph": ("cognee-code", "cognee-architecture"),
    "forget-or-switch": ("cognee-forget", "cognee-switch-datasets"),
    "plugin-or-performance": ("cognee-how-it-works", "cognee-report"),
}

#: Claude Code's own memory files, which each of these memory writers must leave
#: to Claude Code: cognee-remember takes plain "remember this" requests, and a
#: handoff note must not land in CLAUDE.md.
BUILT_IN_MEMORY_FILES = {
    "cognee-remember": ("CLAUDE.md", "MEMORY.md"),
    "cognee-handoff": ("CLAUDE.md",),
}

#: Triggers this bare match far more than one skill means.
GENERIC_TRIGGERS = {"report", "status", "history", "search"}

_KEY = re.compile(r"([a-z][a-z0-9_-]*):(?: (.*))?")
_PLAIN_UNSAFE = re.compile(r"^[-?:,\[\]{}#&*!|>'\"%@`]|: | #|:$")


def parse_frontmatter(text: str) -> dict[str, str]:
    """The YAML subset these files use, read strictly.

    Accepted: ``key: plain text``, ``key: "double quoted"`` and ``key: >-``
    followed by lines indented two spaces, which fold into one line. Anything
    else raises, so what this accepts reads the same to a full YAML parser.
    """
    lines = text.split("\n")
    if lines[0] != "---":
        raise ValueError("frontmatter must open on the first line")
    end = lines.index("---", 1)
    fields: dict[str, str] = {}
    i = 1
    while i < end:
        match = _KEY.fullmatch(lines[i])
        if not match:
            raise ValueError(f"line {i + 1}: expected 'key: value', got {lines[i]!r}")
        key, value = match.group(1), match.group(2) or ""
        if key in fields:
            raise ValueError(f"duplicate key {key!r}")
        i += 1
        if value == ">-":
            folded = []
            while i < end and lines[i].startswith("  "):
                line = lines[i][2:]
                if not line or line != line.strip():
                    raise ValueError(f"line {i + 1}: blank, over-indented or padded")
                folded.append(line)
                i += 1
            if not folded:
                raise ValueError(f"{key}: empty folded scalar")
            fields[key] = " ".join(folded)
        elif value.startswith('"'):
            fields[key] = json.loads(value)  # \" and \\ escape the same way in YAML
        elif value and not _PLAIN_UNSAFE.search(value):
            fields[key] = value
        else:
            raise ValueError(f"{key}: quote this value or fold it with '>-': {value!r}")
    return fields


@functools.lru_cache(maxsize=None)
def frontmatter(skill: str) -> dict[str, str]:
    return parse_frontmatter((PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8"))


def not_for(when_to_use: str) -> str:
    _, found, tail = when_to_use.partition("Not for ")
    assert found, "when_to_use has no 'Not for' clause"
    return tail


def redirects(when_to_use: str) -> set[str]:
    """The skills named in parenthesised redirects after "Not for"."""
    return {
        name
        for redirect in re.findall(r"\(([^)]*)\)", not_for(when_to_use))
        for name in re.findall(r"cognee-[a-z]+(?:-[a-z]+)*", redirect)
    }


def test_the_parser_rejects_what_yaml_reads_differently():
    assert parse_frontmatter('---\nname: a\ndescription: "say \\"hi\\""\n---\n') == {
        "name": "a",
        "description": 'say "hi"',
    }
    assert parse_frontmatter("---\nwhen_to_use: >-\n  one\n  two\n---\nbody")["when_to_use"] == (
        "one two"
    )
    for bad in (
        "---\ndescription: Use for: evaluating\n---\n",  # a nested mapping in YAML
        "---\ndescription: run it # now\n---\n",  # a comment in YAML
        '---\ndescription: "unterminated\n---\n',
        "---\nwhen_to_use: >-\n    over-indented\n---\n",
        "\n---\nname: a\n---\n",  # not on line 1: Claude Code reads no frontmatter
    ):
        with pytest.raises(ValueError):
            parse_frontmatter(bad)


def test_the_overlap_groups_name_real_skills():
    named = {skill for group in OVERLAP_GROUPS.values() for skill in group}
    named |= set(BUILT_IN_MEMORY_FILES)
    assert named <= set(SKILLS), f"renamed or removed: {sorted(named - set(SKILLS))}"


@pytest.mark.parametrize("skill", SKILLS)
def test_frontmatter_parses_and_names_the_directory(skill):
    fields = frontmatter(skill)
    assert fields["name"] == skill
    assert fields.get("description", "").strip()
    assert fields.get("when_to_use", "").strip()


@pytest.mark.parametrize("skill", SKILLS)
def test_listing_text_fits_with_headroom(skill):
    fields = frontmatter(skill)
    listing = len(fields["description"]) + len(fields["when_to_use"])
    assert listing <= LISTING_CAP, "Claude Code would cut this skill's listing text"
    assert len(fields["description"]) <= DESCRIPTION_LIMIT
    assert listing <= LISTING_LIMIT


@pytest.mark.parametrize(
    "skill,neighbour",
    [
        pytest.param(skill, other, id=f"{group}:{skill}->{other}")
        for group, members in OVERLAP_GROUPS.items()
        for skill in members
        for other in members
        if other != skill
    ],
)
def test_overlapping_skills_send_requests_to_each_other(skill, neighbour):
    assert neighbour in redirects(frontmatter(skill)["when_to_use"]), (
        f"{skill} needs a 'Not for ... ({neighbour})' clause"
    )


@pytest.mark.parametrize("skill", sorted(BUILT_IN_MEMORY_FILES))
def test_memory_writers_leave_claude_code_memory_files_alone(skill):
    tail = not_for(frontmatter(skill)["when_to_use"])
    missing = [name for name in BUILT_IN_MEMORY_FILES[skill] if name not in tail]
    assert not missing, f"{skill} must leave {missing} to Claude Code"


@pytest.mark.parametrize("skill", SKILLS)
def test_no_bare_generic_trigger(skill):
    phrases = re.findall(r'"([^"]+)"', frontmatter(skill)["when_to_use"])
    assert phrases, "when_to_use quotes no example requests"
    bare = [phrase for phrase in phrases if phrase.strip(" ?.!").lower() in GENERIC_TRIGGERS]
    assert not bare, f"tie {bare} to Cognee, the session or a topic"


def test_dataset_hint_leaves_a_requested_switch_to_the_switch_skill(
    suite, hook_module, monkeypatch
):
    """The hint offers a one-off search in another dataset. It must stop the
    model from switching on its own without stopping a user who asked."""
    if suite.name != "claude-code":
        pytest.skip("Claude Code dataset hint")
    lookup = load_lookup(suite, hook_module, monkeypatch)
    hint = lookup._format_dataset_hint("agent_sessions", [{"name": "research", "id": "r-1"}])
    assert "Do not switch datasets on your own" in hint
    assert "if the user asks to switch" in hint
    assert "/cognee-memory:cognee-switch-datasets" in hint
    assert "Do not switch datasets." not in hint
    # The switch skill quotes this line to say that a user's request wins over it.
    quoted = re.search(
        r'recall hint "([^"]+)"', frontmatter("cognee-switch-datasets")["when_to_use"]
    )
    assert quoted and quoted.group(1) in hint


# ── routing evals (claude plugin eval, evals/) ──────────────────────────────


def eval_cases(group: str) -> list[Path]:
    return sorted(path.parent for path in (EVALS / group).glob("*/prompt.md"))


ALL_CASES = [case for group in [*SKILLS, "negative"] for case in eval_cases(group)]


def grader(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").split("\n")
    assert lines[0] == "---"
    fields = dict(line.split(": ", 1) for line in lines[1 : lines.index("---", 1)])
    match = fields.get("input_match", "")
    assert match.startswith("'") and match.endswith("'") and "'" not in match[1:-1]
    fields["input_match"] = match[1:-1]
    return fields


def skill_call(skill: str, *, namespaced: bool = True, compact: bool = False) -> str:
    """A Skill tool input as the grader sees it, JSON-encoded."""
    name = f"cognee-memory:{skill}" if namespaced else skill
    return json.dumps({"skill": name}, separators=(",", ":") if compact else None)


@pytest.mark.parametrize("skill", SKILLS)
def test_every_skill_has_two_routing_evals_that_match_only_it(skill):
    cases = eval_cases(skill)
    assert [case.name.startswith(f"{skill}-") for case in cases] == [True, True]
    for case in cases:
        fields = grader(case / "graders" / "skill-fired.md")
        assert (fields["type"], fields["tool"]) == ("tool_used", "Skill")
        assert not {"min", "max"} & set(fields)
        pattern = fields["input_match"]
        assert re.search(pattern, skill_call(skill))
        assert re.search(pattern, skill_call(skill, namespaced=False, compact=True))
        assert not [
            other for other in SKILLS if other != skill and re.search(pattern, skill_call(other))
        ]


def test_negative_routing_evals_forbid_every_cognee_skill():
    cases = eval_cases("negative")
    assert len(cases) == 4
    for case in cases:
        fields = grader(case / "graders" / "no-cognee-skill.md")
        assert (fields["type"], fields["tool"]) == ("tool_used", "Skill")
        assert (fields["min"], fields["max"], fields["arm"]) == ("0", "0", "both")
        assert all(re.search(fields["input_match"], skill_call(skill)) for skill in SKILLS)
        assert not re.search(fields["input_match"], skill_call("init", namespaced=False))


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case.name)
def test_routing_evals_stop_at_the_skill_and_run_isolated(case):
    head = "---\nmax_turns: 2\nallowed_tools: [Skill]\n---\n\n"
    prompt = (case / "prompt.md").read_text(encoding="utf-8")
    assert prompt.startswith(head) and prompt[len(head) :].strip()
    assert (case / "case.yaml").read_text(encoding="utf-8") == (
        f'schema_version: "1.1"\nname: {case.name}\ncontext:\n  scaffold_script: isolate.sh\n'
    )


def test_the_isolation_scaffold_is_one_script_that_never_replaces_a_real_config():
    """The harness passes no COGNEE_* variable from the shell and runs only a
    scaffold inside the case directory, hence one identical copy per case."""
    scripts = {(case / "isolate.sh").read_text(encoding="utf-8") for case in ALL_CASES}
    assert len(ALL_CASES) == 2 * len(SKILLS) + 4 and len(scripts) == 1
    (script,) = scripts
    for setting in (
        "COGNEE_CAPTURE=false",
        "COGNEE_BASE_URL=https://cognee-eval.invalid",
        "COGNEE_IDLE_DISABLED=true",
    ):
        assert f"\n{setting}\n" in script
    assert 'if [ -e "$env_file" ]; then' in script
