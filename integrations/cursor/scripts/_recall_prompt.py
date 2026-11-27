"""Extract user-authored text before automatic recall (CLO-1866).

Host notifications must not spend recall credits or consume coding-memory
turns. Explicit search tools and session-start recall do not use this gate.
"""

from __future__ import annotations

import re
from typing import Callable

_CONTEXT_TAG = re.compile(
    r"\A\s*<(system-reminder|task-notification|scheduled-task|cross-session-message|"
    r"local-command-caveat|local-command-stdout|command-name|command-message|command-args)\b[^>]*>"
)
_CONTEXT_PREFIXES = {
    "[Request interrupted by user": "interrupted",
    "[SUGGESTION MODE:": "suggestion_mode",
}


def recall_user_prompt(payload: dict, on_skip: Callable[[str], None] | None = None) -> str:
    def skip(reason: str) -> str:
        # Reasons are fixed labels, never prompt text or caller-provided roles.
        if on_skip is not None:
            on_skip(reason)
        return ""

    if payload.get("role", "user") != "user":
        return skip("non_user_role")
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        return ""
    for prefix, reason in _CONTEXT_PREFIXES.items():
        if prompt.lstrip().startswith(prefix):
            return skip(reason)
    # Codex's attachment envelope contains a separate, explicit user request.
    if prompt.lstrip().startswith("# Files mentioned by the user"):
        _, separator, request = prompt.partition("## My request for Codex:")
        if not separator or not request.strip():
            return skip("files_context")
        prompt = request.strip()
    user_parts = []
    last_tag = None
    while match := _CONTEXT_TAG.match(prompt):
        tag = match.group(1)
        closing = "</" + tag + ">"
        end = prompt.find(closing, match.end())
        if end < 0:
            return skip(tag)
        if tag == "command-args":
            # Slash-command arguments are authored by the user, even though
            # the host wraps them in the same envelope as command metadata.
            args = prompt[match.end() : end].strip()
            if args:
                user_parts.append(args)
        last_tag = tag
        prompt = prompt[end + len(closing) :].lstrip()
    if user_parts:
        return "\n".join(user_parts + ([prompt] if prompt else []))
    if not prompt and last_tag:
        return skip(last_tag)
    return prompt
