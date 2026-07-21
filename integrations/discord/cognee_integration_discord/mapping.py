"""Pure mapping + formatting helpers shared by every backend.

These are deliberately dependency-free (no discord.py, no cognee) so the
guild→dataset / channel→session convention, the message-provenance header, and
citation formatting can be unit-tested in isolation and reused unchanged when
the shared chat-memory adapter (#3608) lands.
"""

import re

# A Discord message deep-link, e.g.
# https://discord.com/channels/<guild>/<channel>/<message>
_DISCORD_MSG_URL = re.compile(r"https://discord\.com/channels/\d+/\d+/\d+")


def dataset_for_guild(guild_id) -> str:
    """One cognee dataset per Discord server (hard memory-isolation boundary)."""
    return f"discord-guild-{guild_id}"


def session_for_channel(guild_id, channel_id) -> str:
    """One cognee session per channel, for fast conversational recall."""
    return f"discord-{guild_id}-{channel_id}"


def message_url(guild_id, channel_id, message_id) -> str:
    """Build the canonical Discord deep-link for a message (used as a citation)."""
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def format_ingest_text(content: str, url: str, author: str) -> str:
    """Prefix a message with a provenance header so recall can cite its source.

    The header carries the message link and author into the stored text; recall
    surfaces it back and ``extract_citations`` turns it into a Sources list.
    """
    return f"[source] {url} — {author}\n{content}"


def extract_citations(snippets, limit: int = 5) -> list[str]:
    """Collect unique Discord message links found in recalled snippets."""
    seen: list[str] = []
    for snippet in snippets:
        for match in _DISCORD_MSG_URL.findall(snippet or ""):
            if match not in seen:
                seen.append(match)
            if len(seen) >= limit:
                return seen
    return seen


def format_answer(answer: str, citations) -> str:
    """Append a Sources footer of message links to an answer, if any."""
    if not citations:
        return answer
    sources = "\n".join(f"- <{url}>" for url in citations)
    return f"{answer}\n\n**Sources:**\n{sources}"
