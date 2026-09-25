# Cognee Hermes Memory Plugin

The Cognee memory plugin has been installed.

Enable the plugin and configure memory:

```bash
hermes plugins enable cognee
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

## Updates

For a Hermes catalog installation, run `hermes plugins update cognee` to get
the latest reviewed commit. Do not overwrite it with a pip or checkout copy.
Installation by catalog name is available after catalog acceptance.

For a pip installation, run `pip install -U cognee-integration-hermes-agent`
followed by `cognee-hermes-install` to refresh the directory copy. The pip
installer refuses to overwrite a catalog-managed installation.
