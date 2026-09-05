# Configuration precedence

| Integration | Highest precedence → lowest | Details |
|---|---|---|
| Claude Code / Codex | active launch dataset selection → exported environment → `~/.cognee/.env` → defaults | A launch dataset switch overrides the configured dataset for that launch. `COGNEE_CLAUDE_BACKEND` / `COGNEE_CODEX_BACKEND` overrides shared `COGNEE_BACKEND`. Empty env values use defaults. Legacy `config.json` is ignored and removed at startup. |
| Hermes | non-null `HERMES_HOME/cognee.json` → environment → defaults | `COGNEE_BASE_URL` beats deprecated `COGNEE_SERVICE_URL`; `COGNEE_PLUGIN_DATASET` beats `COGNEE_DATASET`. Saved config intentionally wins over exports. Some empty string settings are retained; the provider defaults an empty dataset at use time. |
| OpenClaw | explicit plugin config → supported environment fallbacks → defaults | Credentials accept `${ENV_VAR}` interpolation, which errors for missing variables. `COGNEE_MODE=cloud` forces cloud even with `mode: local`. Boolean plugin settings such as `autoRecall: false` are honored. Arbitrary env names do not override plugin settings. |

Default session dataset: `agent_sessions`. Default local server port: `8011`.

Tests live in the shared Claude/Codex suite, Hermes `test_config_contract.py`, and OpenClaw unit tests. This replaces the obsolete config-file contract proposed in #169.

## Extraction models and authentication

Cognee's backend configures extraction independently of the host assistant.
`LLM_MODEL` passes to the core provider layer; use a model ID documented by that
provider and supported by the installed Cognee/LiteLLM version. Provider-prefixed
IDs such as `anthropic/<provider-model-id>` select that provider; no integration
release is required merely to pass through a new model string. Model/provider
compatibility is tracked in [cognee#4947](https://github.com/topoteretes/cognee/issues/4947).

For hosted APIs, configure a supported API credential in `LLM_API_KEY` and the
backend's provider settings. Local providers can use their supported local
configuration. A remote Cognee server holds its own extraction configuration;
`COGNEE_API_KEY` authenticates the plugin to that server.

Claude Code Pro/Max subscription OAuth tokens authenticate Claude Code itself.
This integration does not extract or reuse those tokens for independent Cognee
LLM calls. Use the provider's supported API or managed-provider authentication,
as described in [Anthropic's authentication terms](https://code.claude.com/docs/en/legal-and-compliance).
