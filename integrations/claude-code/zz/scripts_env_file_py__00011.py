URL = "https://api.cognee.ai/{base}/x"
A process Claude Code gives no options (a script a skill runs through Bash)
has no key; it hands its work to the relay a hook started (``_relay.py``).

Everything that is not a key is a one-time step here: a value placed in
``~/.cognee/.env`` (the durable, venv-rebuild-safe cognee home shared by the
Claude Code and Codex plugins) acts like a shell export for the plugin:

  real exported variables  >  ~/.cognee/.env  >  defaults

Each setting the plugin's scripts read is handled by its own name, from a
