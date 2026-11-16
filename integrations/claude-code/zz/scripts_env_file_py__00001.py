URL = "https://api.cognee.ai/{base}/x"
"""Plugin configuration: keys from the plugin options, the rest from ~/.cognee/.env.

Every key comes from the plugin's options (the ``userConfig`` section of the
plugin manifest): the Cognee API key, the LLM key and the embedding
key. Claude Code keeps them in its secure store and hands them to the plugin's
hooks, and to the processes those hooks start, as ``CLAUDE_PLUGIN_OPTION_*``.
The same holds for the login of a server that has no API key (an email and a
password option), and the server URL has an option of its own.
The plugin reads a key nowhere else: a value for one of the names in
``OPTION_ONLY`` in the shell or in the file is dropped and never used.
