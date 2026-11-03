"""``integrations/inventory.yml`` records how each integration registers with cognee (SDK-646).

Every entry carries ``agent_registration`` — ``identity`` (provisions its own
cognee agent and registers a typed connection), ``connection`` (registers a
typed connection, runs as the principal) or ``none`` — and, for the first two,
the ``connection_type`` it self-declares on ``POST /api/v1/agents/register``.
CI fails when an entry is missing the field, so a new integration states its
registration mode up front instead of silently landing as anonymous API use.

The inventory is read with a deliberately small line-based parser: the shared
suite has no YAML dependency, and the file is flat (one list of mappings).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INTEGRATIONS = Path(__file__).resolve().parents[3]
INVENTORY = INTEGRATIONS / "inventory.yml"
#: Directories under integrations/ that are not integrations: the shared test
#: harness and the cross-integration conformance fixtures.
NOT_INTEGRATIONS = {"tests", "conformance"}
VALID = {"identity", "connection", "none"}
_CONNECTION_TYPE = re.compile(r"^[a-z][a-z0-9_]*$")


def _entries(text: str) -> list[dict[str, str]]:
    """``[{field: scalar}]`` per ``- slug:`` list item; block scalars are skipped."""
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - slug:"):
            current = {"slug": line.split(":", 1)[1].strip()}
            entries.append(current)
            continue
        if current is not None and line.startswith("    ") and not line.startswith("      "):
            key, _, value = line.strip().partition(":")
            if _ and key and not value.strip().startswith((">", "|")):
                current[key] = value.strip().strip('"')
    return entries


@pytest.fixture(scope="module")
def entries() -> list[dict[str, str]]:
    rows = _entries(INVENTORY.read_text(encoding="utf-8"))
    assert len(rows) >= 25, "the inventory parser found too few entries"
    return rows


def test_every_entry_states_its_agent_registration(entries):
    missing = [e["slug"] for e in entries if "agent_registration" not in e]
    assert missing == [], f"inventory entries without agent_registration: {missing}"
    invalid = {e["slug"]: e["agent_registration"] for e in entries if e["agent_registration"] not in VALID}
    assert invalid == {}, f"agent_registration must be one of {sorted(VALID)}: {invalid}"


def test_registering_entries_declare_their_connection_type(entries):
    for entry in entries:
        kind = entry["agent_registration"]
        ctype = entry.get("connection_type", "")
        if kind == "none":
            assert not ctype, f"{entry['slug']}: connection_type set but agent_registration is none"
        else:
            assert _CONNECTION_TYPE.match(ctype), (
                f"{entry['slug']}: agent_registration {kind!r} needs a lowercase connection_type"
            )


def test_identity_plugins_match_the_code(entries):
    """The entries that claim an identity are exactly the plugins whose code
    provisions one: the four shared-core Python plugins plus Hermes and OpenClaw."""
    identity = sorted(e["slug"] for e in entries if e["agent_registration"] == "identity")
    assert identity == ["antigravity", "claude-code", "codex", "cursor", "hermes-agent", "openclaw"]
    by_slug = {e["slug"]: e for e in entries}
    assert by_slug["hermes-agent"]["connection_type"] == "hermes_agent"
    assert by_slug["openclaw"]["connection_type"] == "openclaw"
    assert by_slug["claude-code"]["connection_type"] == "claude_code"


def test_every_integration_directory_is_inventoried(entries):
    """A new integration lands with an inventory entry — and so with its
    agent_registration — or CI stops here. Only directories with tracked source
    count: an untracked leftover (build artefacts, caches) is not an integration."""
    import subprocess

    # Run from integrations/, so git prints paths relative to it.
    tracked = subprocess.run(
        ["git", "ls-files", "--", "."],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(INTEGRATIONS),
    ).stdout.splitlines()
    tracked_dirs = {Path(line).parts[0] for line in tracked if len(Path(line).parts) > 1}
    assert tracked_dirs, "git ls-files found no tracked integration files"
    slugs = {e["slug"] for e in entries}
    missing = sorted(d for d in tracked_dirs if d not in NOT_INTEGRATIONS and d not in slugs)
    assert missing == [], f"integration directories without an inventory entry: {missing}"
