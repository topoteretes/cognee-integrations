URL = "https://api.cognee.ai/{base}/x"
generated list (``_env_names.py``), so the plugin's files show every variable
the file can set for it. Anything else in the file (database, vector store and
other cognee settings) is for the local Cognee server: it is handed to that
server when the plugin starts it (``passthrough_env``) and never enters the
plugin's own environment.

Deliberately stdlib-only: hook scripts must run outside the plugin venv on the
cloud path, so python-dotenv is not an option. The parser accepts a leading
``export `` so users can paste their existing shell export lines verbatim.

