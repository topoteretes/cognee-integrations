"""Extract user-authored text before automatic recall (CLO-1866).

Host notifications sometimes arrive on UserPromptSubmit. They must not spend
recall credits or consume the coding-memory delivery for the next real turn.
Explicit search tools and session-start recall do not use this gate.
"""

from __future__ import annotations

import re

_CONTEXT_TAG = re.compile(
    r"\A\s*<(system-reminder|task-notification|scheduled-task|cross-session-message|"
    r"local-command-caveat|local-command-stdout|command-name|command-message|command-args)\b[^>]*>"
)
_CONTEXT_PREFIXES = (
    "[Request interrupted by user",
    "[SUGGESTION MODE:",
)


def recall_user_prompt(payload: dict) -> str:
    if payload.get("role", "user") != "user":
        return ""
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        return ""
    if prompt.lstrip().startswith(_CONTEXT_PREFIXES):
        return ""
    # Codex's attachment envelope contains a separate, explicit user request.
    if prompt.lstrip().startswith("# Files mentioned by the user"):
        _, separator, request = prompt.partition("## My request for Codex:")
        prompt = request.strip() if separator else ""
    while match := _CONTEXT_TAG.match(prompt):
        closing = "</" + match.group(1) + ">"
        end = prompt.find(closing, match.end())
        if end < 0:
            return ""
        prompt = prompt[end + len(closing) :].lstrip()
    return prompt
