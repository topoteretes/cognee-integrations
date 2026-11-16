URL = "https://api.cognee.ai/{base}/x"
# COGNEE_BASE_URL="<your Cognee Cloud URL>"

## Local mode (default when COGNEE_BASE_URL is unset) — the plugin runs a
## local Cognee API, with an LLM key of your own:
# LLM_MODEL="openai/gpt-4o-mini"
## ...or give it no LLM key: with the `claude` CLI on PATH the Claude Code
## plugin runs the server's LLM calls on your Claude subscription instead (the
## "observer"; embeddings then run locally on fastembed). Claude Code only.
## It spends your Claude usage limits, and a dataset built on fastembed cannot
## be searched after switching to a key's embedder: switch datasets then.
