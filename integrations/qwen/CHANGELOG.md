# Changelog

## Unreleased

- Document that Qwen prompt capture and recall require `submitted_prompt`
  provenance. ACP, headless, `serve`, SDK, remote-input, restored, and Vim-mode
  inputs are intentionally skipped so `ToolResult`/`Hook` continuations cannot
  overwrite or query as user prompts; see
  [Qwen issue #9511](https://github.com/QwenLM/qwen-code/issues/9511). This is
  not an API-key or configuration problem.
- Tool trace, assistant-response, `PreCompact`, and session-sync hooks remain
  active when prompt capture/recall is skipped.

## 1.4.4

- Add the first Qwen Code extension package.
- Capture prompts, tool traces, and assistant responses through Qwen's
  Claude-compatible hooks.
- Recall relevant Cognee context before each prompt and sync at session end.
- Isolate Qwen runtime state and backend selection from other hosts.
- Use the required `/health` probe and tolerate missing optional agent
  lifecycle routes on data-plane-only Cognee servers.
