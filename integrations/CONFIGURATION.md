# Configuration precedence

| Integration | Highest precedence → lowest | Details |
|---|---|---|
| Claude Code / Codex | active launch dataset selection → exported environment → `~/.cognee/.env` → defaults | A launch dataset switch overrides the configured dataset for that launch. `COGNEE_CLAUDE_BACKEND` / `COGNEE_CODEX_BACKEND` overrides shared `COGNEE_BACKEND`. Empty env values use defaults. Legacy `config.json` is ignored and removed at startup. |
| Hermes | non-null `HERMES_HOME/cognee.json` → environment → defaults | `COGNEE_BASE_URL` beats deprecated `COGNEE_SERVICE_URL`; `COGNEE_PLUGIN_DATASET` beats `COGNEE_DATASET`. Saved config intentionally wins over exports. Some empty string settings are retained; the provider defaults an empty dataset at use time. |
| OpenClaw | explicit plugin config → supported environment fallbacks → defaults | Credentials accept `${ENV_VAR}` interpolation, which errors for missing variables. `COGNEE_MODE=cloud` forces cloud even with `mode: local`. Boolean plugin settings such as `autoRecall: false` are honored. Arbitrary env names do not override plugin settings. |

Default session dataset: `agent_sessions`. Default local server port: `8011`.

Tests live in the shared Claude/Codex suite, Hermes `test_config_contract.py`, and OpenClaw unit tests. This replaces the obsolete config-file contract proposed in #169.
