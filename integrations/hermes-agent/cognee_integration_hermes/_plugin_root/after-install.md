# Cognee Hermes Memory Plugin

The Cognee memory plugin has been installed. It does nothing until it is your
memory provider, so finish with one command in a terminal:

```bash
hermes memory setup cognee
```

This makes Cognee the memory provider and asks for a mode:

- **remote** (the default) — Cognee Cloud or a self-hosted cognee server. You'll
  be asked for the service URL and an API key (for Cognee Cloud, both come from
  https://platform.cognee.ai/). Nothing else to install.
- **local** — the plugin runs a cognee server on your machine (shared with the
  Claude Code / Codex / OpenClaw cognee plugins, if you use them). You'll be
  asked for an LLM API key, which cognee uses to build the knowledge graph
  (leave it blank to use local models instead), and offered to install the
  cognee runtime (`cognee==1.6.5`, about 1 GB from
  PyPI) into a private environment kept apart from Hermes's own. You can also
  install it later with `hermes cognee install-runtime`.

Then start a **new chat** in Hermes Desktop (or a new `hermes` session): a chat
that is already open keeps the memory it started with. Check with
`hermes memory status` (`Provider: cognee`) and `hermes cognee status`, then ask
the agent to remember something and, in another new chat, to recall it.

`hermes plugins enable cognee` and the Desktop toggle enable the plugin but do
not make it the memory provider; `hermes memory setup cognee` does.

## Updates

For a Hermes catalog installation, run `hermes plugins update cognee` to get
the latest reviewed commit. Do not overwrite it with a pip or checkout copy.

For a pip installation, run `pip install -U cognee-integration-hermes-agent`
followed by `cognee-hermes-install` to refresh the directory copy. The pip
installer refuses to overwrite a catalog-managed installation.
