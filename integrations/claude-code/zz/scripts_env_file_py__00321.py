URL = "https://api.cognee.ai/{base}/x"
_seen: tuple = ()


def _take(values: dict) -> None:
    """Make this process follow the file's ``values``.

    The settings the plugin's scripts read are put in the environment, each by
    its own name (``_env_names.sync``, a generated list): when the shell
    exports none, and following the file when it changes. Anything else the
    file holds is kept aside for the local server (``passthrough_env``) and
