# Changelog

## 1.4.4

- Add the first Qwen Code extension package.
- Capture prompts, tool traces, and assistant responses through Qwen's
  Claude-compatible hooks.
- Recall relevant Cognee context before each prompt and sync at session end.
- Isolate Qwen runtime state and backend selection from other hosts.
- Use the required `/health` probe and tolerate missing optional agent
  lifecycle routes on data-plane-only Cognee servers.
