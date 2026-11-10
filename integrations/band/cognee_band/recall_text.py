"""Trim an ``only_context`` recall item down to the retrieved context.

On cognee >= 1.6.0 a completion search with ``only_context=true`` returns the
whole prompt the server's own LLM would have received: the conversation
history, the question rendered through the retriever's template, the retrieved
context, and the session guidance block. Only the retrieved context is memory;
the rest repeats the question and adds answering instructions.

Ported from the Claude Code plugin's ``_recall_text.split_only_context``
(SDK-904), with the same two anchors:

* the question marker ``The question is: `<query>```, rendered from the exact
  query the client sent; the context slot opens at the first backtick after it;
* the end: the final character when the text ends with a backtick, otherwise
  the last ``\\n\\n## Active session guidance``.

Text that does not fit (an older server's bare context, a template change) is
returned whole: a parse miss can only cost tokens, never memory.
"""

QUESTION_PREFIX = "The question is: `"
GUIDANCE_TITLE = "## Active session guidance"


def split_only_context(text: str, query: str) -> dict:
    """Split one ``only_context`` item into ``{history, context, guidance, parsed}``."""
    text = str(text or "")
    unparsed = {"history": "", "context": text, "guidance": "", "parsed": False}
    marker = f"{QUESTION_PREFIX}{query}`"
    question_at = text.find(marker)
    if question_at < 0:
        return unparsed
    start = text.find("`", question_at + len(marker))
    if start < 0:
        return unparsed
    start += 1
    if text.endswith("`"):
        end = len(text) - 1
        guidance = ""
    else:
        end = text.rfind("`\n\n" + GUIDANCE_TITLE)
        if end < start:
            return unparsed
        guidance = text[end + 1 :].strip()
    return {
        "history": text[:question_at].rstrip(),
        "context": text[start:end],
        "guidance": guidance,
        "parsed": True,
    }


def retrieved_context(text: str, query: str) -> str:
    """The retrieved context of ``text``, or the whole text when it does not parse."""
    return split_only_context(text, query)["context"].strip()
