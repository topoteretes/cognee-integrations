from cognee_integration_discord import mapping


def test_dataset_and_session_naming():
    assert mapping.dataset_for_guild(42) == "discord-guild-42"
    assert mapping.session_for_channel(42, 7) == "discord-42-7"


def test_message_url():
    assert mapping.message_url(1, 2, 3) == "https://discord.com/channels/1/2/3"


def test_format_ingest_text_has_provenance_header():
    text = mapping.format_ingest_text("hello", "https://discord.com/channels/1/2/3", "alice")
    assert text.startswith("[source] https://discord.com/channels/1/2/3 — alice")
    assert "hello" in text


def test_extract_citations_dedupes_and_limits():
    snippets = [
        "see https://discord.com/channels/1/2/3 and https://discord.com/channels/1/2/3",
        "also https://discord.com/channels/1/2/9",
        "no links here",
    ]
    assert mapping.extract_citations(snippets) == [
        "https://discord.com/channels/1/2/3",
        "https://discord.com/channels/1/2/9",
    ]
    # limit is honored
    many = [f"https://discord.com/channels/1/2/{i}" for i in range(10)]
    assert len(mapping.extract_citations(many, limit=3)) == 3


def test_format_answer_without_citations_returns_answer_unchanged():
    assert mapping.format_answer("just the answer", []) == "just the answer"


def test_format_answer_with_citations_appends_sources():
    out = mapping.format_answer("the answer", ["https://discord.com/channels/1/2/3"])
    assert "the answer" in out
    assert "**Sources:**" in out
    assert "<https://discord.com/channels/1/2/3>" in out
