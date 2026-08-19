# Cognee for Qwen Code

This extension adds automatic Cognee memory to
[Qwen Code](https://github.com/QwenLM/qwen-code):

- session bootstrap on `SessionStart`
- recall and prompt capture on `UserPromptSubmit`
- tool and assistant-response capture
- a pre-compaction memory anchor
- final session-to-graph sync

Qwen uses Gemini-style extension manifests but Claude-style hook event names.
Its command-hook timeouts are milliseconds, so this package deliberately uses
`120000` for a 120-second hook window.

## Install

```bash
qwen extensions install /path/to/cognee-integrations/integrations/qwen
```

For local development:

```bash
qwen extensions link /path/to/cognee-integrations/integrations/qwen
```

The extension ships both `qwen-extension.json` and
`gemini-extension.json`. Installation does not edit
`~/.qwen/settings.json`.

## Configure

Put durable settings in `~/.cognee/.env`, shared with the Claude Code and
Codex plugins:

```dotenv
# Remote Cognee server:
COGNEE_BASE_URL="https://your-instance.cognee.ai"
COGNEE_API_KEY="ck_..."

# Or local mode:
LLM_API_KEY="sk-..."
```

`COGNEE_BACKEND=local|cloud` selects a mode for every Cognee plugin in the
current shell. `COGNEE_QWEN_BACKEND=local|cloud` selects it for Qwen only.
Qwen-specific state lives under `~/.cognee-plugin/qwen/`; the default dataset
is `agent_sessions`.

Update the installed extension with:

```bash
qwen extensions update cognee
```
