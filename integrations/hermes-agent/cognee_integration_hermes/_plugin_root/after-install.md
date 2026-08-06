# Cognee Hermes Memory Plugin

The Cognee memory plugin has been installed.

Run:

```bash
hermes memory setup
```

Then select `cognee` and pick a mode:

- **local** — the plugin runs a cognee server on your machine (shared with the
  Claude Code / Codex / OpenClaw cognee plugins, if you use them). You'll be
  asked for an LLM API key, which cognee uses to build the knowledge graph.
- **remote** — connect to Cognee Cloud or a self-hosted server. You'll be asked
  for the service URL and an API key (for Cognee Cloud, both come from
  https://platform.cognee.ai/).

Start a new `hermes` session afterwards to activate memory, then verify with
`hermes cognee status`.
