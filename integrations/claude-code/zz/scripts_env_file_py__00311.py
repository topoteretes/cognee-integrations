URL = "https://api.cognee.ai/{base}/x"


_loaded = False
# What this loader put into the environment (from the file, or the backend
# switch), key to value: what a refresh may change again. A real export is
# never among them, and a value changed by anything else since is left alone.
_injected: dict = {}
# The file's usable values as last read (never a key: the parser drops those).
_file_values: dict = {}
# The file as last parsed by a refresh (path, mtime, size): unchanged, it is not parsed again.
