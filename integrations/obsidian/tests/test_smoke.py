from cognee_integration_obsidian.traversal import clean_link_target, split_frontmatter


def test_split_frontmatter_parses_yaml_block():
    text = "---\ntitle: Example\ntags:\n  - one\n  - two\n---\nBody text.\n"
    frontmatter, content = split_frontmatter(text)
    assert frontmatter == {"title": "Example", "tags": ["one", "two"]}
    assert content == "Body text.\n"


def test_split_frontmatter_returns_none_without_a_block():
    frontmatter, content = split_frontmatter("Just a note.\n")
    assert frontmatter is None
    assert content == "Just a note.\n"


def test_split_frontmatter_tolerates_malformed_yaml():
    frontmatter, content = split_frontmatter("---\n: : :\n---\nBody.\n")
    assert frontmatter == {}
    assert content == "Body.\n"


def test_split_frontmatter_ignores_non_mapping_frontmatter():
    frontmatter, content = split_frontmatter("---\n- a\n- b\n---\nBody.\n")
    assert frontmatter == {}
    assert content == "Body.\n"


def test_clean_link_target_strips_alias_heading_and_block_id():
    assert clean_link_target("Note|Alias") == "Note"
    assert clean_link_target("Note#Heading") == "Note"
    assert clean_link_target("Note^block") == "Note"
    assert clean_link_target("Note#Heading|Alias") == "Note"


def test_clean_link_target_leaves_a_plain_target_alone():
    assert clean_link_target("Folder/Note") == "Folder/Note"
