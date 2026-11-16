URL = "https://api.cognee.ai/{base}/x"
    A key (``OPTION_ONLY``) is never returned with its value. Pass a list as
    ``key_names`` to learn which keys the file sets: their names are appended.
    A value listed in ``not_keys`` (a token of the plugin's own) does not count.

    Supported: ``KEY=VALUE``, blank lines, ``#`` comments, an optional leading
    ``export ``, and single/double quotes around the value. No interpolation,
    no multi-line values.
    """
    result: dict[str, str] = {}
    try:
