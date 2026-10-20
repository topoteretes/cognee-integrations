"""Static checks for the Grok Bot plugin, which ships no code of its own.

Grok Bot reads Cursor-format plugins: `.cursor-plugin/plugin.json`, which also
declares the MCP server, and `rules/*.mdc`, listed in the repository-root
`.cursor-plugin/marketplace.json`. A typo in any of them fails silently in the
host (the plugin, the server or the rule just does not appear), so check them here.
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
    return json.loads(path.read_text())


def _frontmatter(path: Path) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", path.read_text(), re.DOTALL)
    assert match, f"{path} has no frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def test_manifest_names_the_plugin_and_matches_the_changelog():
    manifest = _json(PLUGIN / ".cursor-plugin/plugin.json")
    assert KEBAB.match(manifest["name"])
    latest = re.search(r"^## \[([^\]]+)\]", (PLUGIN / "CHANGELOG.md").read_text(), re.M)
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
    assert (REPO / entry["source"]).resolve() == PLUGIN.resolve()


def test_mcp_config_points_at_the_hosted_server_without_credentials():
    # Declared inline: Grok Bot did not register the server from a separate
    # mcp.json found by discovery, so the manifest carries it directly.
    servers = _json(PLUGIN / ".cursor-plugin/plugin.json")["mcpServers"]
    assert servers == {"cognee": {"url": HOSTED_MCP_URL}}
    assert not (PLUGIN / "mcp.json").exists()


def test_the_only_component_is_one_always_on_rule():
    # The rule replaces skills: it is in context on every request, the way the
    # Claude Code plugin's SessionStart steer is, so the bot uses memory unasked.
    assert not (PLUGIN / "skills").exists()
    rules = sorted((PLUGIN / "rules").iterdir())
    assert [rule.name for rule in rules] == ["cognee-memory.mdc"]
    # Listed file by file: Grok Bot did not pick the rule up by folder discovery.
    listed = _json(PLUGIN / ".cursor-plugin/plugin.json")["rules"]
    assert [(PLUGIN / path).resolve() for path in listed] == [rules[0].resolve()]
    fields = _frontmatter(rules[0])
    assert fields["alwaysApply"] == "true"
    assert fields["description"]


def test_rule_covers_every_hosted_tool_and_no_local_scripts():
    # The hosted server serves cognee-mcp's tools with cognify_status renamed to
    # check_ingestion_status, and Grok Bot has no plugin scripts to run.
    text = (PLUGIN / "rules/cognee-memory.mdc").read_text()
    for tool in ("remember", "recall", "forget", "check_ingestion_status"):
        assert f"`{tool}`" in text
    assert "dataset_name" in text and "`datasets`" in text
    assert "scripts/" not in text
    assert "cognify_status" not in text


def test_rule_asks_for_a_line_showing_what_cognee_did():
    # Users of the Claude Code plugin did not notice Cognee until each answer named
    # the memories it used; the rule asks for the same, by dataset, with no tags.
    text = (PLUGIN / "rules/cognee-memory.mdc").read_text()
    assert "> 🧠 Cognee memory (<dataset>): " in text
    assert "> 🧠 Saved to Cognee (<dataset>): " in text
