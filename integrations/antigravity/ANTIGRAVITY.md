# Cognee for Antigravity

This plugin gives Antigravity sessions durable Cognee memory. It recalls
relevant session context before invocations and captures prompts, tool results,
and completed responses for later retrieval.

Plugin-specific runtime state is stored in `~/.cognee-plugin/antigravity`.
Cognee configuration, its managed virtual environment, API-key cache, local
server markers, and `~/.cognee` data are shared with other Cognee plugins.

Set `COGNEE_ANTIGRAVITY_BACKEND` to choose this plugin's backend without
changing the shared `COGNEE_BACKEND` setting. Run the bundled Cognee skills for
setup, recall, codebase ingestion, or local UI operations.
