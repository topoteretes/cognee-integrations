"""Static checks for the Grok Bot plugin, which ships no code of its own.

Grok Bot reads Cursor-format plugins: `.cursor-plugin/plugin.json`, which also
declares the MCP server, and `skills/*/SKILL.md`, listed in the repository-root
`.cursor-plugin/marketplace.json`. A typo in any of them fails silently in the
host (the plugin, the server or the skill just does not appear), so check them here.
"""

import json
import re
from pathlib import Path

INTEGRATIONS = Path(__file__).resolve().parents[3]
REPO = INTEGRATIONS.parent
PLUGIN = INTEGRATIONS / "grok-bot"
KEBAB = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
HOSTED_MCP_URL = "https://mcp.aws.cognee.ai/mcp"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _frontmatter(path: Path) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.DOTALL)
    assert match, f"{path} has no frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def test_manifest_names_the_plugin_and_matches_the_changelog():
    manifest = _json(PLUGIN / ".cursor-plugin/plugin.json")
    assert KEBAB.match(manifest["name"])
    changelog = (PLUGIN / "CHANGELOG.md").read_text(encoding="utf-8")
    latest = re.search(r"^## \[([^\]]+)\]", changelog, re.M)
    assert latest and latest.group(1) == manifest["version"]
    # The description shows the version, so it is visible in Grok Bot's plugin list.
    assert manifest["description"].endswith(f"Version {manifest['version']}.")


def test_logo_is_a_committed_file_referenced_by_a_relative_path():
    # Cursor's submission checklist asks for a relative logo path; it rejects
    # absolute paths and `..`, so the file has to live inside the plugin folder.
    logo = _json(PLUGIN / ".cursor-plugin/plugin.json")["logo"]
    assert not logo.startswith(("/", "http://", "https://")) and ".." not in logo
    assert (PLUGIN / logo).is_file()


def test_marketplace_lists_the_plugin_at_its_directory():
    marketplace = _json(REPO / ".cursor-plugin/marketplace.json")
    assert KEBAB.match(marketplace["name"])
    assert marketplace["owner"]["name"]
    manifest = _json(PLUGIN / ".cursor-plugin/plugin.json")
    entries = {entry["name"]: entry for entry in marketplace["plugins"]}
    entry = entries[manifest["name"]]
    # Sources are relative to metadata.pluginRoot when the marketplace sets one.
    root = REPO / marketplace.get("metadata", {}).get("pluginRoot", ".")
    assert (root / entry["source"]).resolve() == PLUGIN.resolve()


def test_mcp_config_points_at_the_hosted_server_without_credentials():
    # Declared inline: Grok Bot did not register the server from a separate
    # mcp.json found by discovery, so the manifest carries it directly.
    servers = _json(PLUGIN / ".cursor-plugin/plugin.json")["mcpServers"]
    assert servers == {"cognee": {"url": HOSTED_MCP_URL}}
    assert not (PLUGIN / "mcp.json").exists()


def test_the_only_component_is_one_memory_skill():
    # A skill, not a rule: Grok Bot's plugins load connectors and skills but drop
    # the manifest's `rules`, so the bot never saw an always-on rule.
    assert not (PLUGIN / "rules").exists()
    assert "rules" not in _json(PLUGIN / ".cursor-plugin/plugin.json")
    skills = sorted((PLUGIN / "skills").iterdir())
    assert [skill.name for skill in skills] == ["cognee-memory"]
    fields = _frontmatter(skills[0] / "SKILL.md")
    assert fields["name"] == "cognee-memory"
    # There is no always-on switch, so the description must ask for every chat.
    assert "start of every conversation" in fields["description"]


def test_skill_description_gates_every_tool_and_carries_the_key_rules():
    # Grok Bot sees only the description until it loads the skill, and called
    # `remember` directly when the description read as optional advice. It names
    # the tools it is required before, and repeats the dataset and 🧠-line rules
    # in case the body is never loaded.
    description = _frontmatter(PLUGIN / "skills/cognee-memory/SKILL.md")["description"]
    assert description.startswith("Required before calling any tool of the cognee MCP server")
    for tool in ("remember", "recall", "forget", "check_ingestion_status"):
        assert tool in description
    assert "dataset" in description and "🧠" in description
    # Skill descriptions are capped at 1024 characters.
    assert len(description) <= 1024


def test_skill_covers_every_hosted_tool_and_no_local_scripts():
    # The hosted server serves cognee-mcp's tools with cognify_status renamed to
    # check_ingestion_status, and Grok Bot has no plugin scripts to run.
    text = (PLUGIN / "skills/cognee-memory/SKILL.md").read_text(encoding="utf-8")
    for tool in ("remember", "recall", "forget", "check_ingestion_status"):
        assert f"`{tool}`" in text
    assert "dataset_name" in text and "`datasets`" in text
    assert "scripts/" not in text
    assert "cognify_status" not in text


def test_skill_asks_for_a_line_showing_what_cognee_did():
    # Users of the Claude Code plugin did not notice Cognee until each answer named
    # the memories it used; the skill asks for the same, by dataset, with no tags.
    text = (PLUGIN / "skills/cognee-memory/SKILL.md").read_text(encoding="utf-8")
    assert "> 🧠 Cognee memory (<dataset>): " in text
    assert "> 🧠 Saved to Cognee (<dataset>): " in text
