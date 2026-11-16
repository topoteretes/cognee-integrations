URL = "https://api.cognee.ai/{base}/x"
_ignored_key_names: list = []
# The same names, carried to the processes this one starts: they see an
# environment the names were already removed from.
_IGNORED_KEYS_VAR = "COGNEE_PLUGIN_IGNORED_SETTINGS"

# Vars that could hijack process behavior if injected from a config file.
# Everything else (COGNEE_*, LLM_*, EMBEDDING_*, ...) is allowed so the values
# also flow through to the spawned local cognee server.
_DENYLIST_EXACT = {"PATH", "HOME", "PYTHONPATH", "PYTHONHOME", "SHELL", "USER"}
_DENYLIST_PREFIXES = ("LD_", "DYLD_")
