# Cognee memory for Qwen Code

This extension captures provenanced prompts, tool traces, and assistant
responses in Cognee session memory, recalls relevant context for provenanced
prompts, and syncs completed sessions into graph memory. `UserPromptSubmit`
events without `submitted_prompt` are intentionally skipped for prompt
capture/recall; tool trace, assistant-response, PreCompact, and sync hooks stay
active. See [Qwen issue #9511](https://github.com/QwenLM/qwen-code/issues/9511).

Runtime configuration lives in `~/.cognee/.env`, shared with the Claude Code
and Codex integrations.

@./skills/memory/SKILL.md
@./skills/setup/SKILL.md
@./skills/codebase/SKILL.md
